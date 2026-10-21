from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ..observations import Observation, observation_for
from ..types import (
    EvidenceRecord,
    RuntimeBlocker,
    TaskCheckpoint,
    ToolAction,
    ToolDefinition,
    ToolResult,
    UserRequest,
    WorkspaceSessionState,
)
from .history import bounded_arguments

if TYPE_CHECKING:
    from ..capabilities.model import InstructionContent
    from ..context import ExplicitContextItem, ProjectRule, WorkspaceSummary


@dataclass(slots=True)
class SessionContext:
    request: UserRequest
    workspace_state: WorkspaceSessionState = field(default_factory=WorkspaceSessionState)
    available_tools: list[ToolDefinition] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    model_history: list[list[dict[str, object]]] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    recovered_observations: list[Observation] = field(default_factory=list)
    visible_observations: frozenset[str] | None = None
    visible_observation_views: list[dict[str, object]] | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    active_instructions: list[InstructionContent] = field(default_factory=list)
    project_rules: list[ProjectRule] = field(default_factory=list)
    workspace_summary: WorkspaceSummary | None = None
    explicit_context: list[ExplicitContextItem] = field(default_factory=list)
    external_tool_statuses: list[dict[str, object]] = field(default_factory=list)
    checkpoint: TaskCheckpoint = field(default_factory=TaskCheckpoint)
    cancellation: threading.Event | None = None
    before_model_request: Callable[[], None] | None = None


    def add_model_message(self, message: str, actions: list[ToolAction] | None = None) -> None:
        self.history.append(f"model: {message}")
        projected: dict[str, object] = {"role": "assistant", "content": message}
        if actions:
            projected["tool_calls"] = [
                {"id": f"call_saiworks_{len(self.model_history)}_{index}", "type": "function",
                 "function": {"name": action.name, "arguments": json.dumps(
                     bounded_arguments(action.arguments), ensure_ascii=False, sort_keys=True)}}
                for index, action in enumerate(actions)
            ]
        self.model_history.append([projected])

    def add_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)
        status = "ok" if result.success else f"error:{result.error_code or 'tool_failed'}"
        arguments = result.metadata.get("action_arguments")
        argument_text = ""
        if arguments:
            visible_arguments = self._bounded_arguments(arguments)
            argument_text = f" args={json.dumps(visible_arguments, ensure_ascii=False, sort_keys=True)}"
        action_artifact_ref = result.metadata.get("action_artifact_ref")
        if isinstance(action_artifact_ref, str) and action_artifact_ref:
            argument_text += f" args_ref={action_artifact_ref}"
        self.history.append(f"tool:{result.name}:{status}{argument_text}: {result.output}")
        self._add_observation(result)
        artifact_refs = self._record_artifacts(result)
        self._record_evidence(result, artifact_refs)
        if result.success:
            self.checkpoint.completed_actions.append(result.name)
            self.checkpoint.completed_actions = self.checkpoint.completed_actions[-50:]
        else:
            self.checkpoint.blockers.append(
                RuntimeBlocker(
                    error_code=result.error_code or "tool_failed",
                    summary=self._bounded_text(result.output),
                    source="tool",
                    tool=result.name,
                    retryability=str(result.metadata.get("retryability", "conditional")),
                    required_action=str(result.metadata.get("required_action", "change_strategy")),
                )
            )
        cwd = result.metadata.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.checkpoint.runtime_state["shell_cwd"] = cwd

    def _record_artifacts(self, result: ToolResult) -> list[str]:
        values: list[str] = []
        artifact_ref = result.metadata.get("artifact_ref")
        if isinstance(artifact_ref, str) and artifact_ref:
            values.append(artifact_ref)
        artifact_refs = result.metadata.get("artifact_refs")
        if isinstance(artifact_refs, list):
            values.extend(item for item in artifact_refs if isinstance(item, str) and item)
        for value in values:
            if value not in self.checkpoint.artifacts:
                self.checkpoint.artifacts.append(value)
        self.checkpoint.artifacts = self.checkpoint.artifacts[-50:]
        return list(dict.fromkeys(values))

    def _record_evidence(self, result: ToolResult, artifact_refs: list[str]) -> None:
        previous_revision = self.checkpoint.workspace_revision
        invalidated = result.metadata.get("invalidates_evidence", [])
        invalidated_kinds = set()
        if isinstance(invalidated, list):
            invalidated_kinds = {item for item in invalidated if isinstance(item, str) and item}
            self.checkpoint.evidence = [
                item
                for item in self.checkpoint.evidence
                if not (
                    item.task_id == self.checkpoint.task_id
                    and item.workspace_revision == self.checkpoint.workspace_revision
                    and item.kind in invalidated_kinds
                )
            ]
        raw = result.metadata.get("evidence", [])
        kinds = [item for item in raw if isinstance(item, str) and item] if isinstance(raw, list) else []
        kinds = list(dict.fromkeys(kinds))
        if "workspace_change" in kinds or (
            result.metadata.get("invalidates_workspace_state") is True
            and "workspace_change" not in kinds
        ):
            self.checkpoint.workspace_revision += 1
        if invalidated_kinds or self.checkpoint.workspace_revision != previous_revision:
            self.checkpoint.revoke_completion()
        if not kinds:
            return
        revision = self.checkpoint.workspace_revision
        raw_sources = result.metadata.get("evidence_sources", {})
        for kind in kinds:
            sources = raw_sources.get(kind, []) if isinstance(raw_sources, dict) else []
            record = EvidenceRecord(
                kind=kind,
                producer=result.name,
                task_id=self.checkpoint.task_id,
                workspace_revision=revision,
                artifact_refs=list(artifact_refs),
                source_task_ids=[
                    item for item in sources if isinstance(item, str) and item
                ] if isinstance(sources, list) else [],
            )
            self.checkpoint.evidence = [
                item
                for item in self.checkpoint.evidence
                if not (
                    item.kind == record.kind
                    and item.producer == record.producer
                    and item.workspace_revision == record.workspace_revision
                )
            ]
            self.checkpoint.evidence.append(record)
        self.checkpoint.evidence = self.checkpoint.evidence[-100:]

    def _bounded_arguments(self, value: object) -> object:
        return bounded_arguments(value)

    def _add_observation(self, result: ToolResult) -> None:
        record = observation_for(result)
        if record is None or record.success != result.success or record.error_code != result.error_code:
            action = ToolAction(result.name, result.metadata.get("action_arguments", {}))
            record = Observation.create(action, result, self.workspace_state.active_root or self.request.cwd)
            if result.error_code in {"progress_required", "model_output_invalid"}:
                record = replace(record, control=True)
            result.metadata["_observation"] = record.payload()
        self.observations.append(record)
        complete = bool(result.metadata.get("_observation_body_complete", record.complete))
        group = self.model_history[-1] if self.model_history else []
        calls = group[0].get("tool_calls", []) if group else []
        completed = {message.get("tool_call_id") for message in group[1:]}
        pending = next((call for call in calls if call["id"] not in completed), None)
        visible_coverage = None
        if not complete and record.coverage is not None and result.name == "read_file":
            start, end = result.metadata.get("start_line"), result.metadata.get("end_line")
            if type(start) is int and type(end) is int:
                visible_coverage = (start, end)
        if pending is not None and pending["function"]["name"] == result.name:
            if result.error_code == "blocked_by_security_policy":
                pending["function"]["arguments"] = "{}"
            projected = record.message(result.output, role="tool", complete=complete,
                                       coverage=visible_coverage, tool_call_id=pending["id"])
            if visible_coverage is not None:
                projected["_visible_coverage"] = visible_coverage
            group.append(projected)
        else:
            # Synthetic runtime feedback has no model-issued call to impersonate.
            projected = record.message(result.output, complete=complete, coverage=visible_coverage)
            if visible_coverage is not None:
                projected["_visible_coverage"] = visible_coverage
            self.model_history.append([projected])

    def _bounded_text(self, value: str, limit: int = 1_000) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."
