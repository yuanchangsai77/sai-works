from __future__ import annotations

from typing import Protocol
from datetime import datetime, timezone

from ..types import ToolAction, ToolResult


class ActionJournal(Protocol):
    def start(self, action: ToolAction, workspace_root: str, risk_level: str) -> str | None: ...

    def finish(self, action_id: str, result: ToolResult, *, known_not_started: bool = False) -> None: ...


class TaskActionJournal:
    """Bind durable action boundaries to one accepted Task submission."""

    def __init__(self, store, task_id: str, submission_id: str) -> None:
        self.store = store
        self.task_id = task_id
        self.submission_id = submission_id

    def recovery_problem(self) -> str | None:
        actions = self.store.recovery_actions(self.task_id)
        if not actions:
            return None
        now = datetime.now(timezone.utc)
        details = []
        for item in actions[-8:]:
            elapsed = max(0, (now - datetime.fromisoformat(item['started_at'])).total_seconds())
            receipt = "no completion receipt recorded" if item['state'] == "started" else "error receipt recorded"
            details.append(
                f"- action_id={item['action_id']}; tool={item['action_name']}; "
                f"receipt={receipt}; error={item['error_code']}; "
                f"started_at={item['started_at']}; elapsed_seconds={elapsed:.1f}"
            )
        if len(actions) > 8:
            details.append(f"{len(actions) - 8} older current records omitted from this summary.")
        return (
            "Previous action execution facts (external effects may remain unverified):\n"
            + "\n".join(details)
            + "\nElapsed time is since dispatch, not proof that a process is still running. "
            "A timeout reports a waiting limit, not an external outcome. "
            "Use these facts and tool output to decide whether to inspect, repair, retry, or summarize. "
            "These records do not block further actions or completion; report unresolved outcomes accurately."
        )

    def start(self, action: ToolAction, workspace_root: str, risk_level: str) -> str | None:
        return self.store.start_action(
            self.task_id, self.submission_id, action.name, action.arguments,
            workspace_root, risk_level,
        )

    def finish(self, action_id: str, result: ToolResult, *, known_not_started: bool = False) -> None:
        self.store.finish_action(action_id, success=result.success, error_code=result.error_code, known_not_started=known_not_started)
