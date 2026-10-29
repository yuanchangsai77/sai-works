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

    def __init__(self, logger, session: SessionContext, refresh_workspace) -> None:
        self.logger = logger
        self.session = session
        self.refresh_workspace = refresh_workspace

    def record(self, result: ToolResult, *, synthetic: bool = False, refresh_workspace: bool = False) -> None:
        if synthetic:
            record_synthetic_tool_result(self.logger, result)
        self.session.add_tool_result(result)
        if refresh_workspace:
            self.refresh_workspace(self.session)
