from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import path_error, resolve_workspace_path, retarget, schema


def tool() -> SimpleTool:
    return SimpleTool(
        name="workspace_open",
        description="Open a directory as the active workspace for this session.",
        arguments={"path": "Workspace-relative or absolute directory path."},
        input_schema=schema({"path": {"type": "string"}}, required=["path"]),
        evidence_kinds=["read"],
        handler=run,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    resolved = resolve_workspace_path(context, action.arguments["path"])
    if isinstance(resolved, ToolResult):
        return retarget(resolved, action.name)
    if error := path_error(action, resolved, "directory"):
        return error
    return ToolResult(
        name=action.name,
        success=True,
        output=f"Active workspace set to {resolved.path}",
        metadata={"path": str(resolved.path)},
    )
