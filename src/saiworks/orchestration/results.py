from __future__ import annotations

from ..types import ToolResult
from .session import SessionContext


def record_synthetic_tool_result(logger, result: ToolResult) -> None:
    event = logger.record(
        "tool.result",
        {
            "name": result.name,
            "success": result.success,
            "output": result.output,
            "error_code": result.error_code,
            "metadata": result.metadata,
        },
    )
    action_ref = getattr(event, "payload", {}).get("arguments_ref", {})
    if isinstance(action_ref, dict) and action_ref.get("artifact_ref"):
        result.metadata.setdefault("action_artifact_ref", action_ref["artifact_ref"])


class ResultRecorder:
    """Project one result into the run session without duplicating real tool logs."""

    def __init__(self, engine, session: SessionContext) -> None:
        self.engine = engine
        self.session = session

    def record(self, result: ToolResult, *, synthetic: bool = False, refresh_workspace: bool = False) -> None:
        if synthetic:
            record_synthetic_tool_result(self.engine.logger, result)
        self.session.add_tool_result(result)
        if refresh_workspace:
            self.engine._refresh_task_workspace_context(self.session)
