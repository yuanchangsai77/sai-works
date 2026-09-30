from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass, is_dataclass
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
    schema_version: int = 1

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


class TaskStore:
    """Atomic acceptance and attempt ownership, independent of legacy session JSON.

    Unfinished claims are never reclaimed automatically after a process restart.
    Their external effects must first be reconciled by an explicit recovery path.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS tasks (
                submission_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                session_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
                bound_context TEXT NOT NULL, state TEXT NOT NULL,
                run_ids TEXT NOT NULL DEFAULT '[]', result TEXT, error TEXT,
                schema_version INTEGER NOT NULL DEFAULT 1
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS tasks_identity ON tasks(task_id)")

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _record(row) -> TaskRecord | None:
        if row is None:
            return None
        if row["schema_version"] != 1:
            raise ValueError("Unsupported task record version")
        result = _restore(ExecutionSummary, json.loads(row["result"])) if row["result"] else None
        return TaskRecord(row["task_id"], row["submission_id"], row["session_id"],
                          row["request_fingerprint"], json.loads(row["bound_context"]),
                          row["state"], json.loads(row["run_ids"]), result, row["error"])

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
                    (submission_id, task_id, session_id, request_fingerprint, bound_context, state)
                    VALUES (?, ?, ?, ?, ?, 'queued')""",
                    (submission_id, task_id or uuid4().hex, str(request.metadata.get("session_id") or ""), fingerprint, encoded))
                record = self._record(connection.execute("SELECT * FROM tasks WHERE submission_id=?", (submission_id,)).fetchone())
        return record

    def get_submission(self, submission_id: str) -> TaskRecord | None:
        with closing(self._connect()) as connection:
            return self._record(connection.execute("SELECT * FROM tasks WHERE submission_id=?", (submission_id,)).fetchone())

    def claim(self, submission_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            return connection.execute("UPDATE tasks SET state='running' WHERE submission_id=? AND state='queued'", (submission_id,)).rowcount == 1

    def finish(self, submission_id: str, summary: ExecutionSummary | None, *, run_id: str | None = None, error: str | None = None) -> None:
        encoded = _json(summary) if summary is not None else None
        state = "cancelled" if summary is not None and summary.outcome == "interrupted" else "finished"
        with closing(self._connect()) as connection, connection:
            changed = connection.execute("""UPDATE tasks SET state=?, result=?, error=?, run_ids=?
                WHERE submission_id=? AND state='running'""",
                (state, encoded, error, _json([run_id] if run_id else []), submission_id)).rowcount
            if changed != 1:
                raise RuntimeError("Task attempt is not running")
