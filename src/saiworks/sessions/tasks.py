from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints
from uuid import uuid4

from ..capabilities.model import InstructionContent
from ..types import ExecutionSummary, SessionResumeState, SessionRunTrace, UserRequest, WorkspaceSessionState


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot persist {type(value).__name__}")


def _json(value) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _restore(annotation, value):
    if value is None or annotation is Any:
        return value
    if is_dataclass(annotation) and isinstance(value, dict):
        hints = get_type_hints(annotation, localns={"InstructionContent": InstructionContent})
        return annotation(**{key: _restore(hints[key], item) for key, item in value.items() if key in hints})
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list and isinstance(value, list):
        return [_restore(args[0], item) for item in value]
    if origin is dict and isinstance(value, dict):
        return {key: _restore(args[1], item) for key, item in value.items()}
    return value


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    submission_id: str
    session_id: str
    request_fingerprint: str
    bound_context: dict
    state: str
    run_ids: list[str]
    result: ExecutionSummary | None = None
    error: str | None = None
    schema_version: int = 2
    delivery_state: str = "unknown"

    def request(self) -> UserRequest:
        context = json.loads(_json(self.bound_context))
        metadata = context["metadata"]
        for key, annotation in (("resume_state", SessionResumeState), ("workspace_state", WorkspaceSessionState)):
            if isinstance(metadata.get(key), dict):
                metadata[key] = _restore(annotation, metadata[key])
        if isinstance(metadata.get("session_trace"), list):
            metadata["session_trace"] = [_restore(SessionRunTrace, item) for item in metadata["session_trace"]]
        metadata["task_id"] = self.task_id
        return UserRequest(context["prompt"], context["cwd"], metadata)


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    revision: int
    state: str
    current_submission_id: str
    submissions: tuple[TaskRecord, ...]


APPROVAL_STATES = frozenset({"pending", "approved", "denied", "expired", "cancelled", "consumed"})
APPROVAL_DECISIONS = frozenset({"approve", "deny"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One persisted approval identity bound to a single concrete action."""

    approval_id: str
    task_id: str
    submission_id: str
    action_name: str
    arguments: dict
    action_fingerprint: str
    workspace_root: str
    required_scope: str
    created_at: str
    expires_at: str
    state: str
    decision_actor: str | None = None
    decided_at: str | None = None


def fingerprint_action(
    task_id: str,
    submission_id: str,
    action_name: str,
    arguments: dict,
    workspace_root: str,
    required_scope: str,
) -> str:
    """Canonical binding of an approval to its Task, action and scope."""
    canonical = _json({
        "task_id": task_id,
        "submission_id": submission_id,
        "action_name": action_name,
        "arguments": arguments,
        "workspace_root": str(Path(workspace_root).resolve()),
        "required_scope": required_scope,
    })
    return hashlib.sha256(canonical.encode()).hexdigest()


class TaskStore:
    """Atomic acceptance and attempt ownership, independent of legacy session JSON.

    Unfinished claims are never reclaimed automatically after a process restart.
    Their external effects must first be reconciled by an explicit recovery path.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1, 2, 3):
                raise ValueError("Unsupported task database version")
            connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
                submission_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                session_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
                bound_context TEXT NOT NULL, state TEXT NOT NULL,
                run_ids TEXT NOT NULL DEFAULT '[]', result TEXT, error TEXT,
                schema_version INTEGER NOT NULL DEFAULT 2
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_identity ON tasks(task_id)")
            if connection.execute("SELECT 1 FROM tasks WHERE schema_version NOT IN (1, 2) LIMIT 1").fetchone():
                raise ValueError("Unsupported task record version")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "delivery_state" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute("CREATE TABLE IF NOT EXISTS task_revisions (task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO task_revisions SELECT DISTINCT task_id, 1 FROM tasks")
            connection.execute("UPDATE tasks SET schema_version=2 WHERE schema_version=1")
            connection.execute("""CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                submission_id TEXT NOT NULL, action_name TEXT NOT NULL,
                arguments TEXT NOT NULL, action_fingerprint TEXT NOT NULL,
                workspace_root TEXT NOT NULL, required_scope TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                state TEXT NOT NULL, decision_actor TEXT, decided_at TEXT
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS approvals_task ON approvals(task_id)")
            connection.execute("""CREATE TABLE IF NOT EXISTS task_cancellations (
                task_id TEXT PRIMARY KEY, accepted_at TEXT NOT NULL
            )""")
            connection.execute("PRAGMA user_version=3")

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _record(row) -> TaskRecord | None:
        if row is None:
            return None
        if row["schema_version"] not in (1, 2):
            raise ValueError("Unsupported task record version")
        result = _restore(ExecutionSummary, json.loads(row["result"])) if row["result"] else None
        return TaskRecord(row["task_id"], row["submission_id"], row["session_id"],
                          row["request_fingerprint"], json.loads(row["bound_context"]),
                          row["state"], json.loads(row["run_ids"]), result, row["error"],
                          row["schema_version"], row["delivery_state"])

    @staticmethod
    def _bump(connection, task_id: str) -> None:
        connection.execute("""INSERT INTO task_revisions VALUES (?, 1)
            ON CONFLICT(task_id) DO UPDATE SET revision=revision+1""", (task_id,))

    def snapshot(self, task_id: str) -> TaskSnapshot:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            revision = connection.execute("SELECT revision FROM task_revisions WHERE task_id=?", (task_id,)).fetchone()
            if revision is None:
                raise KeyError(task_id)
            records = tuple(self._record(row) for row in connection.execute(
                "SELECT * FROM tasks WHERE task_id=? ORDER BY rowid", (task_id,)))
            current = next((record for record in records if record.state == "running"), None)
            current = current or next((record for record in records if record.state == "queued"), records[-1])
            return TaskSnapshot(task_id, revision[0], current.state, current.submission_id, records)

    def set_delivery_state(self, submission_id: str, state: str) -> None:
        if state not in ("pending", "completed"):
            raise ValueError("Invalid delivery state")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT task_id, state, delivery_state FROM tasks WHERE submission_id=?", (submission_id,)).fetchone()
            if row is None:
                raise KeyError(submission_id)
            if row["delivery_state"] == state or row["delivery_state"] == "completed":
                return
            if state == "completed" and row["state"] not in ("finished", "cancelled"):
                raise RuntimeError("Execution result is not committed")
            connection.execute("UPDATE tasks SET delivery_state=? WHERE submission_id=?", (state, submission_id))
            self._bump(connection, row["task_id"])

    def accept(self, request: UserRequest, *, submission_id: str, task_id: str | None = None) -> TaskRecord:
        if not isinstance(submission_id, str) or not submission_id.strip():
            raise ValueError("submission_id must be a nonempty string")
        context = {"cwd": str(Path(request.cwd).resolve()), "prompt": request.prompt,
                   "metadata": dict(request.metadata)}
        for key in ("runtime_model", "last_session_user_message"):
            context["metadata"].pop(key, None)
        encoded = _json(context)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM tasks WHERE submission_id=?", (submission_id,)).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint:
                    raise ValueError("submission_id already belongs to different input")
                record = self._record(existing)
            else:
                if task_id and connection.execute("SELECT 1 FROM tasks WHERE task_id=? AND state='running'", (task_id,)).fetchone():
                    raise RuntimeError("Task has an unfinished attempt requiring explicit recovery")
                connection.execute("""INSERT INTO tasks
                    (submission_id, task_id, session_id, request_fingerprint, bound_context, state, schema_version, delivery_state)
                    VALUES (?, ?, ?, ?, ?, 'queued', 2, 'not_required')""",
                    (submission_id, task_id or uuid4().hex, str(request.metadata.get("session_id") or ""), fingerprint, encoded))
                record = self._record(connection.execute("SELECT * FROM tasks WHERE submission_id=?", (submission_id,)).fetchone())
                self._bump(connection, record.task_id)
        return record

    def get_submission(self, submission_id: str) -> TaskRecord | None:
        with closing(self._connect()) as connection:
            return self._record(connection.execute("SELECT * FROM tasks WHERE submission_id=?", (submission_id,)).fetchone())

    def claim(self, submission_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            changed = connection.execute("UPDATE tasks SET state='running' WHERE submission_id=? AND state='queued'", (submission_id,)).rowcount == 1
            if changed:
                task_id = connection.execute("SELECT task_id FROM tasks WHERE submission_id=?", (submission_id,)).fetchone()[0]
                self._bump(connection, task_id)
            return changed

    def finish(self, submission_id: str, summary: ExecutionSummary | None, *, run_id: str | None = None, error: str | None = None) -> None:
        encoded = _json(summary) if summary is not None else None
        state = "cancelled" if summary is not None and summary.outcome == "interrupted" else "finished"
        with closing(self._connect()) as connection, connection:
            changed = connection.execute("""UPDATE tasks SET state=?, result=?, error=?, run_ids=?
                WHERE submission_id=? AND state='running'""",
                (state, encoded, error, _json([run_id] if run_id else []), submission_id)).rowcount
            if changed != 1:
                raise RuntimeError("Task attempt is not running")
            task_id = connection.execute("SELECT task_id FROM tasks WHERE submission_id=?", (submission_id,)).fetchone()[0]
            self._bump(connection, task_id)

    @staticmethod
    def _approval(row) -> ApprovalRecord | None:
        if row is None:
            return None
        if row["state"] not in APPROVAL_STATES:
            raise ValueError("Unsupported approval record state")
        return ApprovalRecord(
            row["approval_id"], row["task_id"], row["submission_id"], row["action_name"],
            json.loads(row["arguments"]), row["action_fingerprint"], row["workspace_root"],
            row["required_scope"], row["created_at"], row["expires_at"], row["state"],
            row["decision_actor"], row["decided_at"],
        )

    @staticmethod
    def _fresh_approval(connection, approval_id: str) -> ApprovalRecord:
        row = connection.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        return TaskStore._approval(row)

    def request_approval(
        self,
        *,
        task_id: str,
        submission_id: str,
        action_name: str,
        arguments: dict,
        workspace_root: str,
        required_scope: str,
        ttl_seconds: int,
        approval_id: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("approval ttl must be a positive number of seconds")
        moment = now or _utcnow()
        fingerprint = fingerprint_action(task_id, submission_id, action_name, arguments, workspace_root, required_scope)
        approval_id = approval_id or uuid4().hex
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if existing is not None:
                if existing["action_fingerprint"] != fingerprint:
                    raise ValueError("approval_id already belongs to a different action")
                return self._approval(existing)
            if connection.execute("SELECT 1 FROM tasks WHERE submission_id=? AND task_id=?", (submission_id, task_id)).fetchone() is None:
                raise KeyError(submission_id)
            if connection.execute("SELECT 1 FROM task_cancellations WHERE task_id=?", (task_id,)).fetchone() is not None:
                raise RuntimeError("Task cancellation is already accepted; no new approvals")
            connection.execute("""INSERT INTO approvals
                (approval_id, task_id, submission_id, action_name, arguments, action_fingerprint,
                 workspace_root, required_scope, created_at, expires_at, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
                (approval_id, task_id, submission_id, action_name, _json(dict(arguments)), fingerprint,
                 str(Path(workspace_root).resolve()), required_scope,
                 moment.isoformat(), (moment + timedelta(seconds=ttl_seconds)).isoformat()))
            self._bump(connection, task_id)
            return self._fresh_approval(connection, approval_id)

    def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        expected_action_fingerprint: str,
        actor: str,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """Adjudicate one approval in a single authoritative transaction.

        The first committed decision stands: a repeated identical decision is
        idempotent, a conflicting decision returns the standing ruling unchanged,
        and a cancellation accepted earlier invalidates a late decision.
        """
        if decision not in APPROVAL_DECISIONS:
            raise ValueError("decision must be 'approve' or 'deny'")
        moment = now or _utcnow()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
            if row is None:
                raise KeyError(approval_id)
            record = self._approval(row)
            if expected_action_fingerprint != record.action_fingerprint:
                raise ValueError("approval action fingerprint mismatch")
            if record.state != "pending":
                return record
            if connection.execute("SELECT 1 FROM task_cancellations WHERE task_id=?", (record.task_id,)).fetchone() is not None:
                connection.execute("UPDATE approvals SET state='cancelled' WHERE approval_id=?", (approval_id,))
                self._bump(connection, record.task_id)
                return self._fresh_approval(connection, approval_id)
            if datetime.fromisoformat(record.expires_at) <= moment:
                connection.execute("UPDATE approvals SET state='expired' WHERE approval_id=?", (approval_id,))
                self._bump(connection, record.task_id)
                return self._fresh_approval(connection, approval_id)
            changed = connection.execute(
                "UPDATE approvals SET state=?, decision_actor=?, decided_at=? WHERE approval_id=? AND state='pending'",
                ("approved" if decision == "approve" else "denied", actor, moment.isoformat(), approval_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Approval is no longer pending")
            self._bump(connection, record.task_id)
            return self._fresh_approval(connection, approval_id)

    def accept_cancellation(self, task_id: str, now: datetime | None = None) -> str:
        """Record the authoritative cancellation acceptance point once.

        After this point no approval may start a new action; pending approvals
        are cancelled in the same transaction. Repeated calls return the
        original acceptance point.
        """
        moment = now or _utcnow()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT accepted_at FROM task_cancellations WHERE task_id=?", (task_id,)).fetchone()
            if existing is not None:
                return existing[0]
            if connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone() is None:
                raise KeyError(task_id)
            accepted_at = moment.isoformat()
            connection.execute("INSERT INTO task_cancellations (task_id, accepted_at) VALUES (?, ?)", (task_id, accepted_at))
            connection.execute("UPDATE approvals SET state='cancelled' WHERE task_id=? AND state='pending'", (task_id,))
            self._bump(connection, task_id)
            return accepted_at

    def cancellation_accepted(self, task_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT accepted_at FROM task_cancellations WHERE task_id=?", (task_id,)).fetchone()
            return row[0] if row is not None else None

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with closing(self._connect()) as connection:
            return self._approval(connection.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone())

    def pending_approvals(self, task_id: str, now: datetime | None = None) -> tuple[ApprovalRecord, ...]:
        """Live pending approvals; expired rows transition in the same transaction."""
        moment = now or _utcnow()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM approvals WHERE task_id=? AND state='pending' ORDER BY created_at, approval_id",
                (task_id,)).fetchall()
            expired = [row["approval_id"] for row in rows if datetime.fromisoformat(row["expires_at"]) <= moment]
            live = tuple(self._approval(row) for row in rows if row["approval_id"] not in set(expired))
            if expired:
                marks = ",".join("?" for _ in expired)
                connection.execute(f"UPDATE approvals SET state='expired' WHERE approval_id IN ({marks})", expired)
                self._bump(connection, task_id)
            return live
