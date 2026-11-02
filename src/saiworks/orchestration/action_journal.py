from __future__ import annotations

from typing import Protocol

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

    def start(self, action: ToolAction, workspace_root: str, risk_level: str) -> str | None:
        return self.store.start_action(
            self.task_id, self.submission_id, action.name, action.arguments,
            workspace_root, risk_level,
        )

    def finish(self, action_id: str, result: ToolResult, *, known_not_started: bool = False) -> None:
        self.store.finish_action(action_id, success=result.success, error_code=result.error_code, known_not_started=known_not_started)
