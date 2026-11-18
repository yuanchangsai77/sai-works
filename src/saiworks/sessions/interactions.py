"""Durable nonterminal waits, sharing TaskStore's transaction authority."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class PendingInteraction:
    interaction_id: str
    submission_id: str
    kind: str
    question: str
    options: tuple[str, ...]
    state: str


class ApprovalUnavailable(RuntimeError):
    pass


class InteractionStore:
    @staticmethod
    def create_interaction_schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS task_interactions (
            interaction_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, submission_id TEXT NOT NULL,
            kind TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL,
            state TEXT NOT NULL, answer TEXT, created_at TEXT NOT NULL)""")
        connection.execute("CREATE INDEX IF NOT EXISTS interactions_submission ON task_interactions(submission_id)")
        connection.execute("""CREATE TABLE IF NOT EXISTS run_continuations (
            submission_id TEXT PRIMARY KEY, interaction_id TEXT NOT NULL, payload TEXT NOT NULL)""")

    @staticmethod
    def interaction_view(row):
        return PendingInteraction(row['interaction_id'], row['submission_id'], row['kind'],
                                  row['question'], tuple(json.loads(row['options'])), row['state'])

    def suspend(self, submission_id, waiting, *, ttl_seconds=300):
        from .tasks import _json, _utcnow, fingerprint_action
        if waiting.kind == 'approval' and (not isinstance(ttl_seconds, int) or ttl_seconds <= 0):
            raise ValueError('approval ttl must be a positive number of seconds')
        identity, now = uuid4().hex, _utcnow()
        payload = waiting.continuation
        # Serialize everything before changing authoritative state.
        encoded = _json(payload)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            task = connection.execute("SELECT * FROM tasks WHERE submission_id=? AND state='running'", (submission_id,)).fetchone()
            if task is None:
                raise RuntimeError('Only a running submission can suspend')
            if connection.execute('SELECT 1 FROM task_cancellations WHERE task_id=?', (task['task_id'],)).fetchone():
                raise KeyboardInterrupt
            if waiting.kind == 'approval':
                action = waiting.action
                fingerprint = fingerprint_action(task['task_id'], submission_id, action.name,
                                                 action.arguments, waiting.cwd, waiting.scope)
                connection.execute("""INSERT INTO approvals
                    (approval_id,task_id,submission_id,action_name,arguments,action_fingerprint,
                     workspace_root,required_scope,created_at,expires_at,state,decision_actor,decided_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)""",
                    (identity, task['task_id'], submission_id, action.name, _json(action.arguments), fingerprint,
                     str(Path(waiting.cwd).resolve()), waiting.scope, now.isoformat(),
                     (now + timedelta(seconds=ttl_seconds)).isoformat()))
            connection.execute("UPDATE task_interactions SET state='superseded' WHERE submission_id=? AND state='pending'", (submission_id,))
            connection.execute("INSERT INTO task_interactions VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
                (identity, task['task_id'], submission_id, waiting.kind, waiting.question, _json(waiting.options), now.isoformat()))
            connection.execute('INSERT OR REPLACE INTO run_continuations VALUES (?, ?, ?)', (submission_id, identity, encoded))
            connection.execute('UPDATE tasks SET state=? WHERE submission_id=?',
                               ('waiting_approval' if waiting.kind == 'approval' else 'waiting_user', submission_id))
            self._bump(connection, task['task_id'])
        return identity

    def continuation(self, submission_id):
        with closing(self._connect()) as connection:
            row = connection.execute("""SELECT run_continuations.payload, task_interactions.* FROM run_continuations
                JOIN task_interactions USING(interaction_id) WHERE run_continuations.submission_id=?""", (submission_id,)).fetchone()
            if row is None:
                return None
            return {'payload': json.loads(row['payload']), 'interaction': dict(row)}

    def restore_wait(self, submission_id, payload, *, error):
        """Return a failed preparation claim without discarding its continuation."""
        from .tasks import _json
        encoded = _json(payload)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("""SELECT task_interactions.*, tasks.state AS submission_state
                FROM run_continuations JOIN task_interactions USING(interaction_id)
                JOIN tasks ON tasks.submission_id=run_continuations.submission_id
                WHERE run_continuations.submission_id=?""", (submission_id,)).fetchone()
            if row is None or row['submission_state'] != 'running':
                raise RuntimeError('Continuation preparation is no longer owned')
            state = 'waiting_user' if row['kind'] == 'user' else 'waiting_approval'
            if row['state'] in {'answered', 'resolved', 'consumed'}:
                state = 'queued'
            connection.execute('UPDATE run_continuations SET payload=? WHERE submission_id=?', (encoded, submission_id))
            connection.execute('UPDATE tasks SET state=?, error=? WHERE submission_id=?', (state, error, submission_id))
            self._bump(connection, row['task_id'])

    def answer(self, task_id, interaction_id, answer):
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('A nonempty text answer is required')
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM task_interactions WHERE interaction_id=?', (interaction_id,)).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            if row['task_id'] != task_id or row['kind'] != 'user':
                raise PermissionError('Interaction does not belong to this reply capability')
            if connection.execute('SELECT 1 FROM task_cancellations WHERE task_id=?', (task_id,)).fetchone():
                raise RuntimeError('Task is cancelled')
            if row['state'] == 'answered':
                if row['answer'] != answer:
                    raise ValueError('Interaction already has a different answer')
                return self.interaction_view(row)
            if row['state'] != 'pending':
                raise RuntimeError('Interaction is no longer awaiting an answer')
            connection.execute("UPDATE task_interactions SET state='answered', answer=? WHERE interaction_id=?", (answer, interaction_id))
            connection.execute("""UPDATE tasks SET state='queued' WHERE submission_id=? AND state='waiting_user'
                AND EXISTS (SELECT 1 FROM run_continuations WHERE submission_id=tasks.submission_id AND interaction_id=?)""",
                (row['submission_id'], interaction_id))
            self._bump(connection, task_id)
            row = connection.execute('SELECT * FROM task_interactions WHERE interaction_id=?', (interaction_id,)).fetchone()
            return self.interaction_view(row)

    def _can_advance(self, connection, row):
        from .tasks import _utcnow
        if row['state'] in {'finished', 'cancelled'}:
            return True
        if row['state'] == 'running':
            return False
        if connection.execute("""SELECT 1 FROM tasks JOIN run_continuations USING(submission_id)
            WHERE task_id=? AND submission_id!=? AND state NOT IN ('finished','cancelled') LIMIT 1""",
            (row['task_id'], row['submission_id'])).fetchone():
            return False
        if row['session_id'] and connection.execute("""SELECT 1 FROM tasks WHERE session_id=? AND rowid <
            (SELECT rowid FROM tasks WHERE submission_id=?) AND state IN ('queued','running','waiting_approval','waiting_user') LIMIT 1""",
            (row['session_id'], row['submission_id'])).fetchone():
            return False
        if connection.execute('SELECT 1 FROM task_cancellations WHERE task_id=?', (row['task_id'],)).fetchone():
            return True
        if row['state'] == 'queued':
            return True
        wait = connection.execute("""SELECT task_interactions.* FROM task_interactions JOIN run_continuations
            USING(interaction_id) WHERE run_continuations.submission_id=?""", (row['submission_id'],)).fetchone()
        if wait is None:
            return False
        if wait['kind'] == 'user':
            return wait['state'] == 'answered'
        approval = connection.execute('SELECT * FROM approvals WHERE approval_id=?', (wait['interaction_id'],)).fetchone()
        return approval is not None and (approval['state'] != 'pending' or datetime.fromisoformat(approval['expires_at']) <= _utcnow())

    def can_advance(self, submission_id):
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT * FROM tasks WHERE submission_id=?', (submission_id,)).fetchone()
            if row is None:
                raise KeyError(submission_id)
            return self._can_advance(connection, row)

    def approval_for_action(self, task_id, submission_id, action, cwd, scope):
        from .tasks import fingerprint_action
        fingerprint = fingerprint_action(task_id, submission_id, action.name, action.arguments, cwd, scope)
        with closing(self._connect()) as connection:
            row = connection.execute('SELECT * FROM approvals WHERE action_fingerprint=? ORDER BY rowid DESC LIMIT 1', (fingerprint,)).fetchone()
            return self._approval(row)

    def consume_approval(self, approval_id, task_id, submission_id, action_name, arguments, workspace, scope, *, connection=None):
        from .tasks import _utcnow, fingerprint_action
        if connection is None:
            with closing(self._connect()) as owned, owned:
                owned.execute('BEGIN IMMEDIATE')
                return self.consume_approval(approval_id, task_id, submission_id, action_name, arguments, workspace, scope, connection=owned)
        if connection.execute('SELECT 1 FROM task_cancellations WHERE task_id=?', (task_id,)).fetchone():
            raise KeyboardInterrupt
        fingerprint = fingerprint_action(task_id, submission_id, action_name, arguments, workspace, scope)
        changed = connection.execute("""UPDATE approvals SET state='consumed' WHERE approval_id=? AND task_id=?
            AND submission_id=? AND action_fingerprint=? AND state='approved' AND expires_at>?""",
            (approval_id, task_id, submission_id, fingerprint, _utcnow().isoformat())).rowcount
        if changed != 1:
            raise ApprovalUnavailable('Approval is expired, changed, or already consumed')
        connection.execute("UPDATE task_interactions SET state='consumed' WHERE interaction_id=?", (approval_id,))
        self._bump(connection, task_id)
