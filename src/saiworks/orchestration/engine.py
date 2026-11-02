from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from ..intent import RequestIntentClassifier
from ..model.types import ModelProtocolError
from ..observations import observation_for
from ..types import (
    EvidenceRecord,
    ExecutionSummary,
    RuntimeBlocker,
    TaskCheckpoint,
    ToolResult,
    UserRequest,
    WorkspaceSessionState,
)
from .action_journal import ActionJournal
from .actions import ActionEnvironment, ActionFlow
from .control import CompletionPolicy, RunBudgetPolicy
from .ext import ContextLoader
from .failures import runtime_failure_message, runtime_failure_retryability
from .finalize import RunFinalizer
from .model_turn import ModelTurn
from .permissions import PermissionContext
from .progress import (
    DefaultProgressPolicy,
    ProgressPolicy,
    ProgressReporter,
    ProgressSignal,
)
from .results import ResultRecorder, record_synthetic_tool_result
from .run_state import RunState
from .session import SessionContext
from .turn_decision import TurnDecision


@dataclass(slots=True)
class ExecutionContext:
    """Task-bound execution handle; legacy run fields are reset per invocation."""

    task_id: str = ""
    tools: object | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    session: SessionContext | None = None
    tools_closed: bool = False
    terminal_summary: ExecutionSummary | None = None
    approval_wait_seconds: float = 0.0
    action_journal: ActionJournal | None = None


class ExecutionEngine:
    """Coordinates the model-think and tool-execute loop."""

    non_resource_control_tools: ClassVar[set[str]] = {
        "warehouse_list",
        "toolbox_open",
        "capability_activate",
        "capability_release",
        "capability_status",
        "subagent_request_effects",
    }
    subagent_only_tools: ClassVar[set[str]] = {"subagent_request_effects"}

    max_model_retries = 7
    model_retry_delays = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)

    non_retryable_error_codes: ClassVar[set[str]] = {
        "approval_denied",
        "approval_required",
        "blocked_by_security_policy",
        "blocked_by_policy",
        "duplicate_tool_call",
        "delegated_effect_not_allowed",
        "delegated_resource_not_allowed",
        "delegated_capability_upgrade_requested",
        "delegated_contract_invalid",
        "invalid_argument_type",
        "invalid_argument_value",
        "invalid_patch",
        "missing_argument",
        "patch_syntax_error",
        "path_outside_workspace",
        "path_not_found",
        "subagent_blocked",
        "subagent_failed",
        "subagent_partial",
        "test_command_ambiguous",
        "test_command_not_detected",
        "unknown_argument",
    }

    def __init__(
        self,
        model,
        tools,
        guardrails,
        logger,
        context_loaders: list[ContextLoader] | None = None,
        capability_warehouse=None,
        approval_callback=None,
        progress_reporter: ProgressReporter | None = None,
        presenter=None,
        max_model_retries: int = 7,
        model_retry_delays: tuple[float, ...] | None = None,
        max_turns: int = 100,
        max_model_attempts: int = 120,
        max_consecutive_model_timeouts: int = 8,
        max_run_seconds: float = 0.0,
        mcp_server_count: int = 0,
        intent_classifier: RequestIntentClassifier | None = None,
        progress_policy: ProgressPolicy | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.guardrails = guardrails
        self.logger = logger
        self.context_loaders = context_loaders or []
        self.capability_warehouse = capability_warehouse
        self.approval_callback = approval_callback
        self.progress_reporter = progress_reporter or presenter
        self.max_model_retries = max(0, int(max_model_retries))
        self.model_retry_delays = tuple(model_retry_delays or self.model_retry_delays)
        self.max_turns = max(1, int(max_turns))
        self.max_model_attempts = max(1, int(max_model_attempts))
        self.max_consecutive_model_timeouts = max(1, int(max_consecutive_model_timeouts))
        self.max_run_seconds = max(0.0, float(max_run_seconds))
        self.run_budget_policy = RunBudgetPolicy(
            max_model_attempts=self.max_model_attempts,
            max_consecutive_model_timeouts=self.max_consecutive_model_timeouts,
            max_run_seconds=self.max_run_seconds,
        )
        self.completion_policy = CompletionPolicy()
        self.mcp_server_count = max(0, int(mcp_server_count))
        self.intent_classifier = intent_classifier or RequestIntentClassifier()
        self.progress_policy = progress_policy or DefaultProgressPolicy()
        self.max_duplicate_skips = 3
        self._tool_state_session_key: str | None = None
        self._keep_tool_state = False
        self._execution_context = ExecutionContext()
        self._run_state = RunState(started_at=time.monotonic())
        self._execution_lock = threading.Lock()
        self._tool_cleanup_lock = threading.RLock()
        self.last_failure_summary: ExecutionSummary | None = None
        self.current_session: SessionContext | None = None

    @property
    def current_session(self) -> SessionContext | None:
        return self._execution_context.session

    @current_session.setter
    def current_session(self, value: SessionContext | None) -> None:
        self._execution_context.session = value

    def execute(self, request: UserRequest) -> ExecutionSummary:
        return self.execute_in_context(request, ExecutionContext())

    def execute_in_context(self, request: UserRequest, context: ExecutionContext) -> ExecutionSummary:
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("Execution engine is already running")
        try:
            with self._tool_cleanup_lock:
                self._execution_context = context
                self._run_state = RunState(started_at=time.monotonic())
                context.tools = self.tools
                context.session = None
                context.tools_closed = False
                context.terminal_summary = None
                context.approval_wait_seconds = 0.0
                self.last_failure_summary = None
            try:
                self._raise_if_cancelled()
                return self._execute(request)
            except KeyboardInterrupt:
                session = self.current_session
                summary = ExecutionSummary(
                    final_message="Interrupted",
                    tool_results=list(session.tool_results) if session is not None else [],
                    outcome="interrupted",
                    checkpoint=session.checkpoint if session is not None else self._initial_checkpoint(request),
                )
                self.cancel_current_run()
                self.last_failure_summary = self._finish(summary)
                raise
            except Exception as error:
                self.last_failure_summary = self._finish(self._runtime_failure_summary(error))
                raise
        finally:
            self._execution_lock.release()

    def cancel_current_run(self) -> None:
        with self._tool_cleanup_lock:
            self.cancel_execution(self._execution_context)

    def cancel_execution(self, context: ExecutionContext) -> None:
        """Only the bound context may interrupt and clean up its resources."""
        context.cancellation.set()
        with self._tool_cleanup_lock:
            if self._execution_context is not context:
                return
            if self._run_state.tools_closed:
                return
            self._tool_state_session_key = None
            self._close_execution_tools()

    def close_tool_state(self) -> None:
        """Idle-only session cleanup, serialized with execution ownership."""
        if not self._execution_lock.acquire(blocking=False):
            raise RuntimeError("Cannot close tool state while execution is running")
        try:
            with self._tool_cleanup_lock:
                reset = getattr(self.tools, "reset_state", None)
                if callable(reset):
                    reset()
                self._tool_state_session_key = None
        finally:
            self._execution_lock.release()

    def _close_execution_tools(self) -> None:
        with self._tool_cleanup_lock:
            context = self._execution_context
            if self._run_state.tools_closed:
                return
            owned_tools = context.tools if context.tools is not None else self.tools
            reset_state = getattr(owned_tools, "reset_state", None)
            try:
                if callable(reset_state):
                    reset_state()
            except Exception as error:
                try:
                    self.logger.record("tool.cleanup_failed", {"error": str(error)})
                except Exception:
                    pass
                return
            self._run_state.tools_closed = True
            context.tools_closed = True

    def _execute(self, request: UserRequest) -> ExecutionSummary:
        runtime_model = getattr(self.model, "model", "")
        if isinstance(runtime_model, str) and runtime_model:
            request.metadata.setdefault("runtime_model", runtime_model)
        session_key = self._session_key(request)
        self.prepare_session_state(
            session_key,
            request.metadata.get("active_capability_ids", []),
        )
        attach_state = getattr(self.tools, "attach_state", None)
        if callable(attach_state):
            attach_state("active_session_id", session_key)
        self._keep_tool_state = session_key is not None
        permissions = PermissionContext()
        available_tools = self._available_tool_definitions(request)
        provider_statuses = getattr(self.tools, "provider_statuses", list)()
        checkpoint = self._initial_checkpoint(request)
        workspace_state = self._workspace_state(request)
        session_request = UserRequest(
            prompt=request.prompt,
            cwd=workspace_state.active_root,
            metadata=request.metadata,
        )
        session = SessionContext(
            request=session_request,
            workspace_state=workspace_state,
            available_tools=available_tools,
            external_tool_statuses=provider_statuses,
            checkpoint=checkpoint,
        )
        self.current_session = session
        archive = getattr(self.tools, "observation_archive", None)
        if archive is not None:
            traces = request.metadata.get("session_trace", [])
            run_ids = [item.get("run_id", "") if isinstance(item, dict) else getattr(item, "run_id", "")
                       for item in traces] if isinstance(traces, list) else []
            session.recovered_observations = archive.recover(
                run_ids, str(request.metadata.get("session_id") or ""), self._workspace_roots(workspace_state),
            )
        for root in self._workspace_roots(workspace_state):
            permissions.grant_workspace_path(root, scope="session")

        for loader in self.context_loaders:
            loader.load_context(session.request, session)
        if self.capability_warehouse is not None:
            self.capability_warehouse.apply_to_session(session)

        state = self._run_state
        request_intent = self.intent_classifier.classify(
            self._intent_prompt(request),
            request.metadata,
        )
        session.checkpoint.required_evidence = self._required_evidence(request, request_intent)
        session.checkpoint.unmet_deliverables = self._unmet_evidence(session)
        state.started_at = time.monotonic()
        session.cancellation = self._execution_context.cancellation

        model_turn = ModelTurn(self, session, state)
        recorder = ResultRecorder(self.logger, session, self._refresh_task_workspace_context)
        turn_decision = TurnDecision(self, session, state, request_intent, recorder)
        action_environment = ActionEnvironment(
            tools=self.tools,
            guardrails=self.guardrails,
            logger=self.logger,
            progress_reporter_provider=lambda: self.progress_reporter,
            capability_warehouse=self.capability_warehouse,
            approval_callback_provider=lambda: self.approval_callback,
            execution_context=self._execution_context,
            raise_if_cancelled=self._raise_if_cancelled,
            non_retryable_error_codes=frozenset(self.non_retryable_error_codes),
            non_resource_control_tools=frozenset(self.non_resource_control_tools),
            max_duplicate_skips=self.max_duplicate_skips,
        )

        for turn in range(1, self.max_turns + 1):
            state.turn_count = turn
            self._raise_if_cancelled()
            budget_problem = self._run_budget_problem(
                state.started_at,
                state.model_attempts,
                state.consecutive_model_timeouts,
            )
            if budget_problem is not None:
                return self._finish(self._budget_summary(session, budget_problem))
            self._sync_checkpoint_blockers(session)
            session.available_tools = self._available_tool_definitions(request)
            visible_tool_names = {definition.name for definition in session.available_tools}
            expiring_turn_capabilities = (
                self.capability_warehouse.active_ids({"turn"})
                if self.capability_warehouse is not None
                else []
            )
            if self.capability_warehouse is not None:
                self.capability_warehouse.apply_to_session(session)
            reply, budget_problem = model_turn.request()
            if budget_problem is not None:
                return self._finish(self._budget_summary(session, budget_problem))
            sync_visible = getattr(self.tools, "set_visible_observations", None)
            if callable(sync_visible):
                views = session.visible_observation_views
                if views is None:
                    views = []
                    for result in session.tool_results:
                        record = observation_for(result)
                        if record is None:
                            continue
                        complete = bool(result.metadata.get("_observation_body_complete", record.complete))
                        coverage = record.coverage
                        if not complete and record.tool == "read_file":
                            start, end = result.metadata.get("start_line"), result.metadata.get("end_line")
                            coverage = (start, end) if type(start) is int and type(end) is int else None
                        views.append({"observation_id": record.identifier,
                                      "complete": complete, "coverage": coverage})
                sync_visible(views)
            session.add_model_message(reply.message, reply.actions)
            self.logger.record(
                "model.reply",
                {
                    "turn": turn,
                    "message": reply.message,
                    "done": reply.done,
                    "actions": [action.name for action in reply.actions],
                },
            )

            turn_results = ActionFlow(
                action_environment,
                request, session, permissions, state, visible_tool_names, recorder,
            ).process(reply.actions)
            self._raise_if_cancelled()

            summary = turn_decision.evaluate(reply, turn_results, expiring_turn_capabilities)
            if summary is not None:
                return self._finish(summary)

        return self._finish(
            ExecutionSummary(
                final_message="Model exceeded the maximum number of turns without producing a final answer.",
                tool_results=session.tool_results,
                outcome="exhausted",
                active_instructions=session.active_instructions,
            )
        )

    def _raise_if_cancelled(self) -> None:
        if self._execution_context.cancellation.is_set():
            raise KeyboardInterrupt

    def _finish(self, summary: ExecutionSummary) -> ExecutionSummary:
        return RunFinalizer(self).finish(summary)

    def _initial_checkpoint(self, request: UserRequest) -> TaskCheckpoint:
        workspace_root = str(Path(request.cwd).resolve())
        resume_state = request.metadata.get("resume_state")
        previous = getattr(resume_state, "checkpoint", None)
        previous_outcome = getattr(resume_state, "last_outcome", "")
        if previous is None and isinstance(resume_state, dict):
            previous = resume_state.get("checkpoint")
            previous_outcome = str(resume_state.get("last_outcome", ""))
        resumable_outcomes = {
            "blocked",
            "stalled",
            "runtime_error",
            "interrupted",
            "exhausted",
            "failed",
            "model_output_invalid",
        }
        delegated = request.metadata.get("delegated_task")
        delegated_task_id = delegated.get("task_id") if isinstance(delegated, dict) else ""
        requested_task_id = request.metadata.get("task_id") or delegated_task_id
        previous_task_id = (
            previous.task_id if isinstance(previous, TaskCheckpoint)
            else str(previous.get("task_id", "")) if isinstance(previous, dict)
            else ""
        )
        previous_root = (
            previous.workspace_root if isinstance(previous, TaskCheckpoint)
            else str(previous.get("workspace_root", "")) if isinstance(previous, dict)
            else ""
        )
        previous_objective = (
            previous.objective if isinstance(previous, TaskCheckpoint)
            else str(previous.get("objective", "")) if isinstance(previous, dict)
            else ""
        )
        explicit_resume_id = request.metadata.get("resume_task_id")
        normalized_prompt = " ".join(request.prompt.split()).casefold()
        explicit_continuation_prompts = {
            "continue",
            "resume",
            "继续",
            "这里继续",
            "接着",
            "继续处理",
            "接着做",
            "接着处理",
        }
        resume_requested = request.metadata.get("continue_task") is True or (
            isinstance(explicit_resume_id, str) and explicit_resume_id == previous_task_id
        ) or (
            isinstance(requested_task_id, str) and requested_task_id == previous_task_id
        ) or (
            normalized_prompt in explicit_continuation_prompts
        ) or (
            previous_objective
            and " ".join(previous_objective.split()).casefold()
            == normalized_prompt
        )
        can_resume = (
            previous_outcome in resumable_outcomes
            and bool(previous_task_id)
            and resume_requested
            and previous_root == workspace_root
        )
        task_id = previous_task_id if can_resume else (
            str(requested_task_id) if requested_task_id else uuid4().hex
        )
        checkpoint = TaskCheckpoint(
            objective=previous_objective if can_resume else request.prompt,
            task_id=task_id,
            workspace_root=workspace_root,
        )
        if isinstance(previous, TaskCheckpoint) and can_resume:
            checkpoint.completed_actions = list(previous.completed_actions[-50:])
            checkpoint.artifacts = list(previous.artifacts[-50:])
            checkpoint.workspace_revision = previous.workspace_revision
            checkpoint.evidence = list(previous.evidence[-100:])
            checkpoint.runtime_state = dict(previous.runtime_state)
        elif isinstance(previous, dict) and can_resume:
            completed = previous.get("completed_actions", [])
            artifacts = previous.get("artifacts", [])
            evidence = previous.get("evidence", [])
            runtime_state = previous.get("runtime_state", {})
            try:
                checkpoint.workspace_revision = max(0, int(previous.get("workspace_revision", 0)))
            except (TypeError, ValueError):
                checkpoint.workspace_revision = 0
            if isinstance(completed, list):
                checkpoint.completed_actions = [str(item) for item in completed[-50:]]
            if isinstance(artifacts, list):
                checkpoint.artifacts = [str(item) for item in artifacts[-50:]]
            if isinstance(evidence, list):
                for item in evidence[-100:]:
                    if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                        continue
                    try:
                        revision = max(0, int(item.get("workspace_revision", 0)))
                    except (TypeError, ValueError):
                        revision = 0
                    checkpoint.evidence.append(
                        EvidenceRecord(
                            kind=item["kind"],
                            producer=str(item.get("producer", "unknown")),
                            task_id=str(item.get("task_id", task_id)),
                            workspace_revision=revision,
                            artifact_refs=[
                                str(ref) for ref in item.get("artifact_refs", [])
                                if isinstance(ref, str) and ref
                            ] if isinstance(item.get("artifact_refs", []), list) else [],
                            source_task_ids=[
                                str(value) for value in item.get("source_task_ids", [])
                                if isinstance(value, str) and value
                            ] if isinstance(item.get("source_task_ids", []), list) else [],
                        )
                    )
            if isinstance(runtime_state, dict):
                checkpoint.runtime_state = {
                    str(key): str(value) for key, value in runtime_state.items()
                }
        shell = getattr(self.tools, "state_for", lambda *_args: None)("shell_session")
        shell_cwd = getattr(shell, "cwd", None)
        if shell_cwd is not None:
            checkpoint.runtime_state["shell_cwd"] = str(shell_cwd)
        else:
            checkpoint.runtime_state["shell_cwd"] = request.cwd
        checkpoint.transition_to("in_progress")
        checkpoint.blockers = []
        return checkpoint

    def _completion_problem(self, message: str, session: SessionContext | None = None) -> str:
        unresolved = (
            [
                result
                for result in self._unresolved_results(session.tool_results)
                if result.name != "completion_gate"
            ]
            if session is not None
            else []
        )
        return self.completion_policy.completion_problem(message, session, unresolved)

    def _required_evidence(self, request: UserRequest, request_intent) -> list[str]:
        return self.completion_policy.required_evidence(request, request_intent)

    def _unmet_evidence(self, session: SessionContext) -> list[str]:
        return self.completion_policy.unmet_evidence(session)

    def _sync_checkpoint_blockers(self, session: SessionContext) -> None:
        unresolved = self._unresolved_results(session.tool_results)
        if not unresolved:
            session.checkpoint.blockers = []
            return
        placeholder = ExecutionSummary("", session.tool_results, outcome="stalled")
        session.checkpoint.blockers = self._runtime_blockers(unresolved, placeholder)

    def _runtime_failure_summary(self, error: BaseException) -> ExecutionSummary:
        session = self.current_session
        results = list(session.tool_results) if session is not None else []
        checkpoint = session.checkpoint if session is not None else TaskCheckpoint()
        message = runtime_failure_message(error)
        blocker = RuntimeBlocker(
            error_code=self._runtime_error_code(error),
            summary=str(error) or type(error).__name__,
            source="runtime",
            retryability=runtime_failure_retryability(error),
            required_action="resume",
        )
        if checkpoint.phase != "in_progress":
            checkpoint.transition_to("in_progress")
        checkpoint.blockers = [blocker]
        return ExecutionSummary(
            final_message=message,
            tool_results=results,
            outcome="runtime_error",
            blockers=[blocker],
            checkpoint=checkpoint,
        )

    def _run_budget_problem(
        self,
        run_started: float,
        model_attempts: int,
        consecutive_model_timeouts: int,
    ) -> RuntimeBlocker | None:
        return self.run_budget_policy.problem(
            run_started + self._run_state.approval_wait_seconds,
            model_attempts,
            consecutive_model_timeouts,
        )

    def _budget_summary(
        self,
        session: SessionContext,
        blocker: RuntimeBlocker,
    ) -> ExecutionSummary:
        return ExecutionSummary(
            final_message=blocker.summary,
            tool_results=session.tool_results,
            outcome="exhausted",
            active_instructions=session.active_instructions,
            blockers=[blocker],
            checkpoint=session.checkpoint,
        )

    def _is_timeout_error(self, error: BaseException) -> bool:
        return self.run_budget_policy.is_timeout(error)

    def _runtime_error_code(self, error: BaseException) -> str:
        current: BaseException | None = error
        while current is not None:
            if isinstance(current, ModelProtocolError):
                return "model_protocol_error"
            name = type(current).__name__
            if name == "ModelTimeoutError" or isinstance(current, TimeoutError):
                return "model_timeout"
            if name == "ModelConnectionError":
                return "model_connection_error"
            if name == "ModelServiceError":
                return "model_service_error"
            current = current.__cause__
        return "runtime_error"

    def _unresolved_results(self, results: list[ToolResult]) -> list[ToolResult]:
        later_successes: set[str] = set()
        unresolved: list[ToolResult] = []
        advisory_error_codes = {
            "progress_required",
            "capability_toolbox_unavailable",
        }
        for result in reversed(results):
            if result.success:
                later_successes.add(self._result_identity(result))
                continue
            if result.error_code in advisory_error_codes:
                continue
            if self._result_identity(result) in later_successes:
                continue
            unresolved.append(result)
        return list(reversed(unresolved))

    def _runtime_blockers(
        self,
        unresolved: list[ToolResult],
        summary: ExecutionSummary,
    ) -> list[RuntimeBlocker]:
        blockers = [
            RuntimeBlocker(
                error_code=result.error_code or "tool_failed",
                summary=self._bounded_blocker_summary(result.output),
                source="tool",
                tool=result.name,
                retryability=(
                    "non_retryable"
                    if result.error_code in self.non_retryable_error_codes
                    else "conditional"
                ),
                required_action="change_strategy",
            )
            for result in unresolved
        ]
        if blockers:
            return blockers[-10:]
        return [
            RuntimeBlocker(
                error_code=summary.outcome,
                summary=summary.final_message,
                source="runtime",
                retryability="conditional",
                required_action="resume",
            )
        ]

    def _bounded_blocker_summary(self, value: str, limit: int = 1_000) -> str:
        if len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _terminal_outcome(self, results: list[ToolResult]) -> str:
        if not any(not result.success for result in results):
            return "completed"
        return self._blocked_outcome(results, default="stalled")

    def _aggregate_outcome(self, results: list[ToolResult]) -> str:
        """Resolve failures by later success of the same tool, not unrelated activity."""
        return self._terminal_outcome(self._unresolved_results(results))

    def _result_identity(self, result: ToolResult) -> str:
        arguments = result.metadata.get("action_arguments")
        return json.dumps(
            {"name": result.name, "arguments": arguments if isinstance(arguments, dict) else None},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _blocked_outcome(self, results: list[ToolResult], *, default: str = "stalled") -> str:
        error_codes = {result.error_code for result in results if not result.success}
        if error_codes & {
            "approval_required",
            "approval_denied",
            "blocked_by_policy",
            "blocked_by_security_policy",
            "delegated_effect_not_allowed",
            "delegated_resource_not_allowed",
            "delegated_capability_upgrade_requested",
            "delegated_contract_invalid",
            "path_outside_workspace",
            "subagent_blocked",
            "subagent_partial",
        }:
            return "blocked"
        if error_codes & {"duplicate_tool_call", "progress_required"}:
            return "stalled"
        return default

    def _available_tool_definitions(self, request: UserRequest):
        definitions = self.tools.definitions()
        contract = request.metadata.get("delegated_task")
        if not isinstance(contract, dict):
            return [item for item in definitions if item.name not in self.subagent_only_tools]
        effects = contract.get("allowed_effects")
        if not isinstance(effects, list):
            return [
                definition
                for definition in definitions
                if definition.risk_level == "read" or definition.name in self.subagent_only_tools
            ]
        allowed = {value for value in effects if isinstance(value, str)}
        return [
            definition
            for definition in definitions
            if definition.risk_level in allowed or definition.name in self.subagent_only_tools
        ]

    def _session_key(self, request: UserRequest) -> str | None:
        session_id = request.metadata.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        return None

    def _intent_prompt(self, request: UserRequest) -> str:
        if request.prompt.strip().casefold() not in {"继续", "continue", "go on", "接着"}:
            return request.prompt
        state = request.metadata.get("resume_state")
        previous = getattr(state, "last_user_prompt", "")
        if isinstance(state, dict):
            previous = state.get("last_user_prompt", "")
        return str(previous).strip() or request.prompt

    def prepare_session_state(
        self,
        session_key: str | None,
        active_capability_ids: Iterable[str] = (),
    ) -> None:
        """Switch runtime tool state to one session and restore its persisted capabilities."""
        self._prepare_tool_state(session_key)
        if self.capability_warehouse is not None:
            self.capability_warehouse.restore_capabilities(active_capability_ids)

    def _prepare_tool_state(self, session_key: str | None) -> None:
        with self._tool_cleanup_lock:
            reset_state = getattr(self.tools, "reset_state", None)
            if not callable(reset_state):
                return
            if session_key is None:
                reset_state()
                self._tool_state_session_key = None
                return
            if self._tool_state_session_key != session_key:
                reset_state()
                self._tool_state_session_key = session_key

    def _all_non_retryable(self, results: list[ToolResult]) -> bool:
        return all(
            not result.success and result.error_code in self.non_retryable_error_codes
            for result in results
        )

    def _has_failed_test_result(self, results: list[ToolResult]) -> bool:
        definition_for = getattr(self.tools, "definition_for", lambda _name: None)
        return any(
            not result.success
            and getattr(definition_for(result.name), "risk_level", "") == "test"
            for result in results
        )

    @staticmethod
    def _workspace_state(request: UserRequest) -> WorkspaceSessionState:
        raw = request.metadata.get("workspace_state")
        if isinstance(raw, WorkspaceSessionState):
            origin = raw.origin_root or request.cwd
            active = raw.active_root or origin
            roots = raw.approved_roots
        else:
            origin = request.cwd
            active = request.cwd
            roots = []
        origin_path = Path(origin).expanduser().resolve(strict=False)
        active_path = Path(active).expanduser().resolve(strict=False)
        approved_roots: list[str] = []
        for root in roots if isinstance(roots, list) else []:
            if isinstance(root, str) and root:
                candidate = str(Path(root).expanduser().resolve(strict=False))
                if candidate not in approved_roots:
                    approved_roots.append(candidate)
        allowed_roots = [origin_path, *(Path(root) for root in approved_roots)]
        if not active_path.is_dir() or not any(
            active_path.is_relative_to(root) for root in allowed_roots
        ):
            active_path = origin_path
        return WorkspaceSessionState(
            origin_root=str(origin_path),
            active_root=str(active_path),
            approved_roots=approved_roots,
        )

    @staticmethod
    def _workspace_roots(state: WorkspaceSessionState) -> list[str]:
        roots = [state.origin_root, *state.approved_roots]
        return list(dict.fromkeys(root for root in roots if root))

    def _refresh_task_workspace_context(self, session: SessionContext) -> None:
        root = session.workspace_state.active_root
        if not root or session.request.cwd == root:
            return
        session.request.cwd = root
        for loader in self.context_loaders:
            loader.load_context(session.request, session)

    @staticmethod
    def _attach_action_metadata(result: ToolResult, action) -> None:
        ActionFlow._attach_action_metadata(result, action)

    def _record_synthetic_tool_result(self, result: ToolResult) -> None:
        record_synthetic_tool_result(self.logger, result)

    def _progress_recovery_result(self, signal: ProgressSignal) -> ToolResult:
        return ToolResult(
            name="progress_guard",
            success=False,
            output=(
                "Repeated inspection is producing no new information. Use the available observations "
                "to deliver the requested analysis or authorized changes. Inspect again only to fill "
                "a specific missing fact. This feedback does not authorize file changes."
            ),
            error_code="progress_required",
            metadata={"repeated_actions": signal.repeated_actions},
        )

    def _non_retryable_failure_message(self, results: list[ToolResult]) -> str:
        reasons = []
        has_permission_issue = False
        for result in results:
            if result.error_code == "path_outside_workspace":
                has_permission_issue = True
                reasons.append("requested path is outside the current workspace")
            elif result.error_code == "approval_required":
                has_permission_issue = True
                reasons.append(f"tool '{result.name}' requires explicit approval")
            elif result.error_code == "approval_denied":
                reasons.append(f"tool '{result.name}' was declined by the user")
            elif result.error_code == "duplicate_tool_call":
                reasons.append(f"tool '{result.name}' repeated the same action without using the previous result")
            else:
                reasons.append(result.output)
        unique_reasons = list(dict.fromkeys(reasons))
        if has_permission_issue:
            prefix = "Cannot continue with the available tool permissions: "
        else:
            prefix = "Cannot continue because tool use stopped making progress: "
        return prefix + "; ".join(unique_reasons) + "."
