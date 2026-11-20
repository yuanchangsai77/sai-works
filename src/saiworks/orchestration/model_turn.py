from __future__ import annotations

from copy import deepcopy
import json
import time

from ..model.types import ModelDecisionError, ModelRetryableError
from ..model.parser import validate_actions
from ..types import RuntimeBlocker
from .run_state import RunState
from .session import SessionContext

MAX_DECISION_CORRECTIONS = 3


class RunBudgetReached(BaseException):
    """Stop auxiliary model requests at the existing run budget boundary."""

    def __init__(self, blocker: RuntimeBlocker) -> None:
        self.blocker = blocker


class ModelTurn:
    """One model request cycle, including retry, budget and stream reporting."""

    def __init__(self, engine, session: SessionContext, state: RunState) -> None:
        self.engine = engine
        self.session = session
        self.state = state
        session.before_model_request = self.before_request

    def budget_problem(self) -> RuntimeBlocker | None:
        state = self.state
        return self.engine._run_budget_problem(
            state.started_at, state.model_attempts, state.consecutive_model_timeouts,
        )

    def before_request(self) -> None:
        self.engine._raise_if_cancelled()
        problem = self.budget_problem()
        if problem is not None:
            raise RunBudgetReached(problem)
        self.state.model_attempts += 1
        self.session.checkpoint.runtime_state["model_attempts"] = str(self.state.model_attempts)

    def request(self):
        engine = self.engine
        state = self.state
        # A correction is still the same decision turn. Registry changes or
        # adapter mutations must not widen its advertised tools or schemas.
        turn_tools = deepcopy(self.session.available_tools)
        schemas = {tool.name: deepcopy(tool.input_schema) for tool in turn_tools}
        reporter = engine.progress_reporter
        progress_handle = reporter.model_started() if reporter else None
        stream_observer_setter = getattr(engine.model, "set_natural_language_stream_observer", None)
        stream_reporter = getattr(reporter, "model_stream_delta", None)
        if callable(stream_observer_setter):
            if callable(stream_reporter):
                stream_observer_setter(
                    lambda delta: stream_reporter(progress_handle, delta.channel, delta.text)
                )
            else:
                stream_observer_setter(None)
        reply = None
        budget_problem = None
        started = time.monotonic()
        try:
            retry_count = 0
            correction_count = 0
            while True:
                if retry_count > 0:
                    retry_reporter = getattr(reporter, "model_retrying", None)
                    if retry_reporter is not None:
                        retry_reporter(
                            progress_handle, retry_count, engine.max_model_retries,
                            "Sending request", 0.0,
                        )
                try:
                    recovery = getattr(engine._execution_context.action_journal, "recovery_problem", None)
                    self.session.recovery_facts = (recovery() or "") if callable(recovery) else ""
                    for loader in engine.context_loaders:
                        refresh = getattr(loader, "refresh_observed_paths", None)
                        if callable(refresh):
                            refresh(self.session.request, self.session)
                    budget_problem = self.budget_problem()
                    if budget_problem is not None:
                        break
                    if not getattr(engine.model, "tracks_model_requests", False):
                        self.before_request()
                    self.session.available_tools = deepcopy(turn_tools)
                    try:
                        reply = engine.model.respond(self.session)
                    finally:
                        self.session.available_tools = deepcopy(turn_tools)
                    try:
                        validate_actions(reply.actions, schemas)
                    except ModelDecisionError as error:
                        error.tool_calls = [
                            {"id": f"call_rejected_{state.turn_count}_{index}", "type": "function",
                             "function": {"name": action.name, "arguments": json.dumps(action.arguments, ensure_ascii=False)}}
                            for index, action in enumerate(reply.actions)
                        ]
                        raise
                    self.session.model_correction = []
                    acknowledge = getattr(engine.tools, 'acknowledge_runtime_state', None)
                    if callable(acknowledge):
                        acknowledge()
                    self.session.checkpoint.runtime_state.pop('shell_recovery', None)
                    state.consecutive_model_timeouts = 0
                    engine._raise_if_cancelled()
                    elapsed_problem = self.budget_problem()
                    if elapsed_problem is not None and elapsed_problem.error_code == "run_time_budget_exhausted":
                        budget_problem = elapsed_problem
                    break
                except RunBudgetReached as error:
                    budget_problem = error.blocker
                    break
                except ModelDecisionError as error:
                    reply = None
                    engine._raise_if_cancelled()
                    state.consecutive_model_timeouts = 0
                    self.session.checkpoint.runtime_state["consecutive_model_timeouts"] = "0"
                    rejected = error
                    rejected.available_tools = sorted(schemas)
                    engine.logger.record("model.decision_rejected", {
                        "request_id": rejected.request_id,
                        "task_id": engine._execution_context.task_id,
                        "error_code": rejected.code, "message": str(rejected),
                        "details": rejected.details, "tool_calls": rejected.tool_calls,
                        "available_tools": rejected.available_tools, "batch_executed": False,
                        "correction": correction_count, "max_corrections": MAX_DECISION_CORRECTIONS,
                    })
                    if correction_count >= MAX_DECISION_CORRECTIONS:
                        raise ModelDecisionError(
                            f"Model decision correction exhausted after {MAX_DECISION_CORRECTIONS} corrections: {error}",
                            code="model_decision_correction_exhausted",
                            details={"last_error_code": rejected.code},
                        ) from error
                    correction_count += 1
                    profile = getattr(engine.model, "capability_profile", None)
                    self.session.model_correction = rejected.correction_messages(
                        native_tools=getattr(profile, "action_protocol", "native_tools") == "native_tools",
                    )
                    correction_reporter = getattr(reporter, "model_retrying", None)
                    if callable(correction_reporter):
                        correction_reporter(progress_handle, correction_count, MAX_DECISION_CORRECTIONS,
                                            "Correcting rejected model decision", 0.0)
                except (ModelRetryableError, TimeoutError) as error:
                    if engine._is_timeout_error(error):
                        state.consecutive_model_timeouts += 1
                    else:
                        state.consecutive_model_timeouts = 0
                    self.session.checkpoint.runtime_state["consecutive_model_timeouts"] = str(
                        state.consecutive_model_timeouts
                    )
                    budget_problem = self.budget_problem()
                    if budget_problem is not None:
                        break
                    if retry_count >= engine.max_model_retries:
                        raise RuntimeError(
                            f"All {engine.max_model_retries} retry attempts failed "
                            f"after the initial request: {error}"
                        ) from error
                    retry_count += 1
                    retry_delay = engine.model_retry_delays[
                        min(retry_count - 1, len(engine.model_retry_delays) - 1)
                    ]
                    engine.logger.record(
                        "model.retry",
                        {
                            "retry": retry_count,
                            "max_retries": engine.max_model_retries,
                            "delay_seconds": retry_delay,
                            "reason": str(error),
                        },
                    )
                    retry_reporter = getattr(reporter, "model_retrying", None)
                    if retry_reporter is not None:
                        retry_reporter(
                            progress_handle, retry_count, engine.max_model_retries,
                            getattr(error, "retry_status", "Model request timed out"), retry_delay,
                        )
                    if engine._execution_context.cancellation.wait(retry_delay):
                        raise KeyboardInterrupt
        finally:
            state.model_seconds += max(0.0, time.monotonic() - started)
            if callable(stream_observer_setter):
                stream_observer_setter(None)
            if progress_handle is not None:
                response_ready = getattr(reporter, "model_response_ready", None)
                if callable(response_ready) and reply is not None:
                    response_ready(progress_handle, reply.message, reply.done and not reply.actions)
                reporter.model_finished(progress_handle)
        return reply, budget_problem
