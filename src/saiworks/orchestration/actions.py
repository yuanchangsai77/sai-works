from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..observations import observation_for
from ..types import ToolAction, ToolResult, UserRequest
from .permissions import PermissionContext
from .results import ResultRecorder
from .run_state import RunState
from .session import SessionContext


@dataclass(frozen=True, slots=True)
class ActionEnvironment:
    """Only the services needed to admit and execute a model action."""

    tools: object
    guardrails: object
    logger: object
    progress_reporter_provider: Callable[[], object]
    capability_warehouse: object
    approval_callback_provider: Callable[[], object]
    execution_context: object
    raise_if_cancelled: Callable[[], None]
    non_retryable_error_codes: frozenset[str]
    non_resource_control_tools: frozenset[str]
    max_duplicate_skips: int

    @property
    def progress_reporter(self):
        return self.progress_reporter_provider()

    @property
    def approval_callback(self):
        return self.approval_callback_provider()


class ActionFlow:
    """Process one model turn's actions in the existing security-check order.

    ToolRegistry owns real execution logging. This flow owns the single
    projection of each returned or synthetic result into the run session.
    """

    def __init__(
        self,
        environment: ActionEnvironment,
        request: UserRequest,
        session: SessionContext,
        permissions: PermissionContext,
        state: RunState,
        visible_names: set[str],
        recorder: ResultRecorder,
    ) -> None:
        self.environment = environment
        self.request = request
        self.session = session
        self.permissions = permissions
        self.state = state
        self.visible_names = visible_names
        self.recorder = recorder
        self.results: list[ToolResult] = []

    def process(self, actions: Iterable[ToolAction]) -> list[ToolResult]:
        for action in actions:
            self.environment.raise_if_cancelled()
            self._process_one(action)
        return self.results

    def _record(self, result: ToolResult, *, synthetic: bool = False, refresh: bool = False) -> None:
        self.recorder.record(result, synthetic=synthetic, refresh_workspace=refresh)
        self.results.append(result)

    def _process_one(self, action: ToolAction) -> None:
        services = self.environment
        state = self.state
        cwd = self._task_workspace_root(self.request, self.session)
        if action.name == "workspace_open":
            state.clear_action_cache()

        if action.name not in self.visible_names:
            definition = services.tools.definition_for(action.name)
            result = self._delegated_effect_problem(definition, self.request)
            if result is None:
                result = ToolResult(
                    name=action.name,
                    success=False,
                    output=(
                        f"tool was not visible at the start of this model turn: {action.name}. "
                        "A newly activated capability can be used on the next model turn."
                    ),
                    error_code="tool_not_visible_this_turn",
                )
            self._attach_action_metadata(result, action)
            self._record(result, synthetic=True)
            return

        key = self._action_key(action)
        previous = state.completed_actions.get(key)
        cache_check = getattr(services.tools, "cached_result_valid", None)
        if previous is not None:
            observation = observation_for(previous)
            invisible = (
                previous.success
                and previous.metadata.get("execution_risk") == "read"
                and self.session.visible_observations is not None
                and (observation is None or observation.identifier not in self.session.visible_observations)
            )
            if invisible or (
                callable(cache_check) and not cache_check(previous=previous, action=action, cwd=cwd)
            ):
                state.completed_actions.pop(key, None)
                state.duplicate_counts.pop(key, None)
                previous = None
        if previous is not None:
            count = state.duplicate_counts.get(key, 0) + 1
            state.duplicate_counts[key] = count
            state.duplicate_skips += 1
            result = self._duplicate_result(action, previous, count)
            if services.progress_reporter:
                services.progress_reporter.tool_skipped(action, "duplicate skipped")
            self._record(result, synthetic=True)
            return

        preflight = getattr(services.tools, "preflight", None)
        if callable(preflight):
            result = preflight(
                action,
                cwd=cwd,
                allowed_roots=self.permissions.workspace_roots(scopes={"run", "session"}),
            )
            if result is not None:
                self._attach_action_metadata(result, action)
                if services.progress_reporter:
                    services.progress_reporter.tool_skipped(action, "blocked by preflight")
                self._record(result, synthetic=True)
                if result.error_code in services.non_retryable_error_codes:
                    state.completed_actions[key] = result
                return

        definition = services.tools.definition_for(action.name)
        result = self._delegated_scope_problem(action, definition, self.request)
        if result is not None:
            self._attach_action_metadata(result, action)
            self._record(result, synthetic=True)
            state.completed_actions[key] = result
            return

        decision = services.guardrails.check(action, definition)
        if decision.requires_confirmation:
            approval_key = (cwd, decision.risk_level, key)
            approval = (
                True
                if self._approval_remembered(approval_key, state.approved_actions)
                else self._approval_decision(action, decision.reason)
            )
            if approval is not True:
                result = ToolResult(
                    name=action.name,
                    success=False,
                    output="Tool execution was declined by the user." if approval is False else decision.reason,
                    error_code="approval_denied" if approval is False else "approval_required",
                )
                if services.progress_reporter:
                    services.progress_reporter.tool_skipped(action, "denied by user")
                self._attach_action_metadata(result, action)
                self._record(result, synthetic=True)
                return
            if decision.risk_level != "destructive":
                state.approved_actions.add(approval_key)
        elif not decision.allowed:
            result = ToolResult(
                name=action.name,
                success=False,
                output=decision.reason,
                error_code=decision.error_code or "blocked_by_policy",
            )
            if services.progress_reporter:
                services.progress_reporter.tool_skipped(action, f"blocked: {decision.reason}")
            self._attach_action_metadata(result, action)
            self._record(result)
            return

        started = time.monotonic()
        try:
            result, action_id = self._execute_action(
                action, cwd, self.permissions, self.session, decision.risk_level,
            )
        finally:
            state.tool_seconds += max(0.0, time.monotonic() - started)
        self._record(result, refresh=True)
        if result.success or result.error_code in services.non_retryable_error_codes:
            if result.success and self._may_mutate_workspace(decision.risk_level):
                state.clear_action_cache()
                if "workspace_change" in result.metadata.get("evidence", []):
                    state.progress_recovery_sent = False
            state.completed_actions[key] = result
        if action_id is not None:
            services.execution_context.action_journal.finish(action_id, result)

    def _may_mutate_workspace(self, risk_level: str) -> bool:
        return risk_level in {"write", "execute", "test", "destructive"}

    def _delegated_scope_problem(self, action, definition, request: UserRequest) -> ToolResult | None:
        contract = request.metadata.get("delegated_task")
        if not isinstance(contract, dict) or definition is None:
            return None
        resources = contract.get("allowed_resources", ["."])
        if not isinstance(resources, list) or "." in resources:
            return None
        candidates: list[str] = []
        for key in ("path", "cwd"):
            value = action.arguments.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        if action.name == "patch":
            diff = action.arguments.get("diff")
            if isinstance(diff, str):
                for line in diff.splitlines():
                    if line.startswith(("+++ b/", "--- a/")):
                        candidates.append(line[6:].strip())
        if not candidates and action.name in self.environment.non_resource_control_tools:
            return None
        if not candidates:
            return ToolResult(
                action.name,
                False,
                "delegated action does not identify a resource covered by its task contract",
                "delegated_resource_not_allowed",
            )

        root = Path(request.cwd).resolve()
        allowed_roots = [(root / str(value)).resolve() for value in resources if isinstance(value, str)]
        for candidate in candidates:
            resolved = Path(candidate)
            if not resolved.is_absolute():
                resolved = root / resolved
            resolved = resolved.resolve()
            if not any(resolved == allowed or resolved.is_relative_to(allowed) for allowed in allowed_roots):
                return ToolResult(
                    action.name,
                    False,
                    f"resource is outside the delegated task contract: {candidate}",
                    "delegated_resource_not_allowed",
                    metadata={"resource": candidate, "allowed_resources": list(resources)},
                )
        return None

    def _delegated_effect_problem(self, definition, request: UserRequest) -> ToolResult | None:
        contract = request.metadata.get("delegated_task")
        if definition is None or not isinstance(contract, dict):
            return None
        effects = contract.get("allowed_effects")
        if not isinstance(effects, list):
            return ToolResult(
                definition.name,
                False,
                "delegated task contract is missing a valid allowed_effects list",
                "delegated_contract_invalid",
            )
        if definition.risk_level in effects:
            return None
        return ToolResult(
            definition.name,
            False,
            f"delegated task does not allow {definition.risk_level} effects",
            "delegated_effect_not_allowed",
            metadata={"requested_effect": definition.risk_level},
        )

    def _approval_remembered(self, approval_key: tuple[str, str, str], approved_actions: set[tuple[str, str, str]]) -> bool:
        return approval_key[1] != "destructive" and approval_key in approved_actions

    def _approval_decision(self, action, reason: str) -> bool | None:
        if self.environment.approval_callback is None:
            return None
        started = time.monotonic()
        try:
            approved = self.environment.approval_callback(action, reason)
        finally:
            waited = max(0.0, time.monotonic() - started)
            self.state.approval_wait_seconds += waited
            self.environment.execution_context.approval_wait_seconds = self.state.approval_wait_seconds
        self.environment.logger.record(
            "safety.approval",
            {"tool": action.name, "approved": approved, "reason": reason, "wait_seconds": waited},
        )
        return bool(approved)

    def _execute_action(
        self,
        action,
        cwd: str,
        permissions: PermissionContext,
        session: SessionContext,
        risk_level: str,
    ) -> tuple[ToolResult, str | None]:
        progress_handle = None
        result = None
        action_id = None
        if self.environment.progress_reporter:
            progress_handle = self.environment.progress_reporter.tool_started(action.name)
        try:
            self.environment.raise_if_cancelled()
            journal = self.environment.execution_context.action_journal
            if journal is not None:
                action_id = journal.start(action, cwd, risk_level)
            if action_id is not None:
                self.environment.logger.record("action.admitted", {
                    "action_id": action_id, "name": action.name,
                    "workspace": cwd, "risk_level": risk_level,
                    "arguments": action.arguments,
                })
            self.state.real_tool_actions += 1
            result = self.environment.tools.execute(
                action,
                cwd=cwd,
                allowed_roots=permissions.workspace_roots(scopes={"run", "session"}),
            )
            self._attach_action_metadata(result, action)
            if result.error_code == "path_outside_workspace":
                if progress_handle is not None:
                    self.environment.progress_reporter.tool_finished(progress_handle, action, result)
                    progress_handle = None

                grant_path = self._workspace_grant_path(result)
                if grant_path is not None:
                    scope = "session"
                    approval = ToolAction(
                        name="workspace_access",
                        arguments={
                            "path": grant_path,
                            "scope": scope,
                            "requested_by": action.name,
                            "original_arguments": dict(action.arguments),
                        },
                    )
                    reason = (
                        f"tool '{action.name}' requested access outside the current workspace: {grant_path}. "
                        "Approve only if this path is part of the task."
                    )
                    approval_decision = self._approval_decision(approval, reason)
                    if approval_decision is not True:
                        ret_res = ToolResult(
                            name=action.name,
                            success=False,
                            output=(
                                "Access outside the workspace was declined by the user."
                                if approval_decision is False
                                else reason
                            ),
                            error_code=(
                                "approval_denied" if approval_decision is False else "approval_required"
                            ),
                            metadata={
                                "action_arguments": dict(action.arguments),
                                "path": grant_path,
                                "requested_by": action.name,
                            },
                        )
                        return ret_res, action_id

                    grant = permissions.grant_workspace_path(grant_path, scope=scope)
                    self._record_workspace_grant(session, grant.path)
                    self.environment.logger.record(
                        "workspace.grant",
                        {"path": grant.path, "scope": grant.scope, "requested_by": action.name},
                    )
                    if self.environment.progress_reporter:
                        progress_handle = self.environment.progress_reporter.tool_started(action.name)
                    self.environment.raise_if_cancelled()
                    retried = self.environment.tools.execute(
                        action,
                        cwd=cwd,
                        allowed_roots=permissions.workspace_roots(scopes={"run", "session"}),
                    )
                    self._attach_action_metadata(retried, action)
                    retried.metadata["workspace_grant"] = grant.path
                    retried.metadata["workspace_grant_scope"] = grant.scope
                    retried.metadata["workspace_grant_source"] = "orchestration"
                    result = retried
            if result is not None and result.success and action.name == "workspace_open":
                self._activate_workspace(session, result)
            if self.environment.capability_warehouse is not None and result is not None:
                self.environment.capability_warehouse.mark_used(
                    action.name,
                    success=result.success,
                    error_code=result.error_code,
                )
            return result, action_id
        finally:
            if progress_handle is not None:
                if result is None:
                    self.environment.progress_reporter.tool_aborted(progress_handle)
                else:
                    self.environment.progress_reporter.tool_finished(progress_handle, action, result)

    def _workspace_grant_path(self, result: ToolResult) -> str | None:
        raw_path = result.metadata.get("resolved_path") or result.metadata.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        return str(Path(raw_path).expanduser().resolve(strict=False))

    @staticmethod
    def _activate_workspace(session: SessionContext, result: ToolResult) -> None:
        raw_path = result.metadata.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return
        candidate = Path(raw_path).expanduser().resolve(strict=False)
        if candidate.is_dir():
            session.workspace_state.active_root = str(candidate)
            session.checkpoint.workspace_root = str(candidate)

    @staticmethod
    def _task_workspace_root(request: UserRequest, session: SessionContext) -> str:
        return session.workspace_state.active_root or request.cwd

    @staticmethod
    def _record_workspace_grant(session: SessionContext, path: str) -> None:
        candidate = str(Path(path).expanduser().resolve(strict=False))
        if candidate not in session.workspace_state.approved_roots:
            session.workspace_state.approved_roots.append(candidate)

    def _action_key(self, action) -> str:
        return json.dumps(
            {"name": action.name, "arguments": action.arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _attach_action_metadata(result: ToolResult, action) -> None:
        if result.error_code == "blocked_by_security_policy":
            return
        result.metadata.setdefault("action_arguments", dict(action.arguments))

    def _duplicate_result(self, action, previous: ToolResult, count: int) -> ToolResult:
        # Read cache reuse is not a repeated side effect. The whole-turn
        # observation policy owns read stagnation, including mixed tool turns.
        read_reuse = previous.success and previous.metadata.get("execution_risk") == "read"
        blocked = not previous.success or (not read_reuse and count > self.environment.max_duplicate_skips)
        return ToolResult(
            name=action.name,
            success=not blocked,
            output=(
                "duplicate tool call skipped because the same action already ran. "
                f"Use the previous result instead of retrying it: {previous.output}"
            ),
            error_code="duplicate_tool_call" if blocked else None,
            metadata={
                **{key: previous.metadata[key] for key in (
                    "execution_workspace", "execution_risk", "path", "start_line", "end_line",
                    "_observation", "_observation_body_complete",
                ) if key in previous.metadata},
                "duplicate": True,
                "duplicate_count": count,
                "duplicate_limit": self.environment.max_duplicate_skips,
                "skipped": True,
                "action_arguments": dict(action.arguments),
                "previous_output": previous.output,
            },
        )
