from __future__ import annotations

from pathlib import Path

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..observation_adapters import revision_observation
from ..shared import run_command, schema
from ..summary import git_show_summary
from .git_common import ensure_git_repository, git_failure


def tool() -> SimpleTool:
    return SimpleTool(
        name="git_show",
        description="Show a git revision or revision:path value.",
        arguments={"revision": "Revision expression to show."},
        input_schema=schema({"revision": {"type": "string"}}, required=["revision"]),
        evidence_kinds=["read"],
        handler=run,
        summarizer=git_show_summary,
        observation_adapter=revision_observation,
    )


def run(action: ToolAction, context: ToolContext) -> ToolResult:
    cwd = Path(context.cwd)
    repository_error = ensure_git_repository(cwd, action.name)
    if repository_error is not None:
        return repository_error

    revision = str(action.arguments["revision"])
    result = run_command(["git", "show", "--stat", "--patch", revision], cwd, shell=False, max_output_bytes=None)
    if not result.success:
        return git_failure(action.name, result, "revision_not_found", "git show failed")

    resolved = run_command(["git", "rev-parse", "--verify", revision], cwd, shell=False, max_output_bytes=None)
    output = result.metadata.get("stdout", "")
    return ToolResult(
        name=action.name,
        success=True,
        output=output or "no output",
        metadata={
            "exit_code": result.metadata.get("exit_code"),
            "revision": revision,
            "resolved_revision": str(resolved.metadata.get("stdout", "")).strip() if resolved.success else "",
            "truncated": result.metadata.get("truncated", False),
            "stdout": output,
            "stderr": result.metadata.get("stderr", ""),
        },
    )
