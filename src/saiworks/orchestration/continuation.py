"""Versioned data for resuming a model turn without replaying completed actions."""
from __future__ import annotations

from dataclasses import asdict
import time
from pathlib import Path

from ..context.explicit import ExplicitContextItem
from ..context.project_rules import ProjectRule
from ..context.workspace import WorkspaceSummary
from ..observations import Observation
from ..sessions.tasks import _restore
from ..types import ModelReply, TaskCheckpoint, ToolAction, ToolDefinition, ToolResult, UserRequest, WorkspaceSessionState
from ..capabilities.model import InstructionContent
from .permissions import PermissionContext
from .run_state import RunState
from .session import SessionContext


class WaitRequested(BaseException):
    """Internal suspension, deliberately neither a failure nor a terminal result."""

    def __init__(self, kind, action, question, cwd, scope="", options=()):
        self.kind, self.action, self.question = kind, action, question
        self.cwd, self.scope, self.options = cwd, scope, list(options)
        self.remaining_actions = []
        self.continuation = None


def capture(session, state, permissions, reply, remaining, results, capability_ids):
    simple = ("history", "model_history", "capability_catalog", "external_tool_statuses")
    typed = ("request", "workspace_state", "checkpoint", "tool_results", "available_tools", "project_rules",
             "workspace_summary", "explicit_context", "active_instructions")
    data = {name: getattr(session, name) for name in (*simple, *typed)}
    data["observations"] = [item.payload() for item in session.observations]
    data["recovered_observations"] = [item.payload() for item in session.recovered_observations]
    run = asdict(state)
    run.pop("terminal_summary")
    run.pop("started_at")
    run["approved_actions"] = list(state.approved_actions)
    return {"version": 1, "session": data, "run": run,
            "elapsed": max(0.0, time.monotonic() - state.started_at - state.approval_wait_seconds),
            "permissions": [asdict(item) for item in permissions._grants],
            "reply": asdict(reply), "remaining_actions": [asdict(item) for item in remaining],
            "turn_results": results, "capability_ids": capability_ids}


def restore(data, cancellation):
    if data.get("version") != 1:
        raise ValueError("Unsupported continuation version")
    raw = data["session"]
    request_data = raw["request"]
    session = SessionContext(request=UserRequest(**request_data), cancellation=cancellation)
    types = {"workspace_state": WorkspaceSessionState, "checkpoint": TaskCheckpoint,
             "available_tools": list[ToolDefinition],
             "tool_results": list[ToolResult], "project_rules": list[ProjectRule],
             "workspace_summary": WorkspaceSummary, "explicit_context": list[ExplicitContextItem],
             "active_instructions": list[InstructionContent]}
    for name, value in raw.items():
        if name == "request":
            continue
        if name in {"observations", "recovered_observations"}:
            setattr(session, name, [item for raw_item in value if (item := Observation.from_payload(raw_item)) is not None])
        else:
            setattr(session, name, _restore(types[name], value) if name in types else value)
    values = dict(data["run"])
    values["approved_actions"] = {tuple(item) for item in values["approved_actions"]}
    values["completed_actions"] = _restore(dict[str, ToolResult], values["completed_actions"])
    values["approval_wait_seconds"] = 0.0
    values["tools_closed"] = False
    state = RunState(**values, started_at=time.monotonic() - data["elapsed"])
    permissions = PermissionContext()
    valid_roots = []
    for grant in data["permissions"]:
        # Stored grants already name canonical targets. Never follow a replacement
        # symlink into a target that was not approved.
        if str(Path(grant["path"]).resolve(strict=False)) == grant["path"]:
            permissions.grant_workspace_path(grant["path"], scope=grant["scope"])
            valid_roots.append(grant["path"])
    session.workspace_state.approved_roots = [
        root for root in session.workspace_state.approved_roots if root in valid_roots
    ]
    return (session, state, permissions, _restore(ModelReply, data["reply"]),
            _restore(list[ToolAction], data["remaining_actions"]), _restore(list[ToolResult], data["turn_results"]))
