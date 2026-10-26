from __future__ import annotations

from ..types import ExecutionSummary, ToolResult
from .progress import DefaultProgressPolicy, ProgressContext
from .results import ResultRecorder
from .run_state import RunState
from .session import SessionContext


class TurnDecision:
    """Decide whether observed turn facts permit completion or require another turn."""

    def __init__(self, engine, session: SessionContext, state: RunState, request_intent, recorder: ResultRecorder) -> None:
        self.engine = engine
        self.session = session
        self.state = state
        self.request_intent = request_intent
        self.recorder = recorder

    def _feedback(self, result: ToolResult) -> None:
        self.recorder.record(result, synthetic=True)

    def _summary(self, message: str, outcome: str) -> ExecutionSummary:
        return ExecutionSummary(
            final_message=message,
            tool_results=self.session.tool_results,
            outcome=outcome,
            active_instructions=self.session.active_instructions,
        )

    def evaluate(self, reply, turn_results: list[ToolResult], expiring_turn_capabilities) -> ExecutionSummary | None:
        engine = self.engine
        session = self.session
        state = self.state
        recovery_required = any(
            result.error_code in {
                "blocked_by_security_policy", "file_changed_since_read", "file_not_read",
            }
            for result in turn_results
        )
        if reply.done and not recovery_required:
            session.checkpoint.unmet_deliverables = engine._unmet_evidence(session)
            completion_problem = engine._completion_problem(reply.message, session)
            if completion_problem:
                state.invalid_completion_count += 1
                self._feedback(ToolResult(
                    name="completion_gate",
                    success=False,
                    output=completion_problem,
                    error_code="model_output_invalid",
                    metadata={
                        "retryability": "conditional",
                        "required_action": "provide_meaningful_final_answer",
                    },
                ))
                if state.invalid_completion_count >= 2:
                    return self._summary(completion_problem, "stalled")
                return None
            if state.invalid_completion_count:
                self._feedback(ToolResult(
                    name="completion_gate",
                    success=True,
                    output="Model supplied a valid replacement completion.",
                ))
            return self._summary(reply.message, engine._terminal_outcome(turn_results))

        if expiring_turn_capabilities and engine.capability_warehouse is not None:
            engine.capability_warehouse.release(
                expiring_turn_capabilities, reason="turn scope consumed",
            )

        if engine._has_failed_test_result(turn_results):
            state.consecutive_failed_test_turns += 1
            if state.consecutive_failed_test_turns >= 3:
                return self._summary(
                    "Stopping after 3 consecutive failing test runs. Review the latest test output before retrying.",
                    "stalled",
                )
        elif turn_results:
            state.consecutive_failed_test_turns = 0

        previous_results = session.tool_results[:-len(turn_results)] if turn_results else session.tool_results
        last_change = next(
            (index for index in range(len(previous_results) - 1, -1, -1)
             if "workspace_change" in previous_results[index].metadata.get("evidence", [])), -1,
        )
        previous_results = previous_results[last_change + 1:]
        if DefaultProgressPolicy.has_new_observation(turn_results, previous_results):
            state.progress_recovery_sent = False
        progress_signal = engine.progress_policy.evaluate(ProgressContext(
            intent=self.request_intent,
            results=turn_results,
            recovery_sent=state.progress_recovery_sent,
            previous_results=previous_results,
            visible_observations=session.visible_observations,
        ))
        if progress_signal is not None:
            if state.progress_recovery_sent:
                return self._summary(
                    "Repeated inspection produced no new information after recovery. Resume from the recorded observations and findings.",
                    "stalled",
                )
            self._feedback(engine._progress_recovery_result(progress_signal))
            state.progress_recovery_sent = True
            state.consecutive_non_retryable_turns = 0
            return None

        if turn_results and engine._all_non_retryable(turn_results):
            state.consecutive_non_retryable_turns += 1
            if state.consecutive_non_retryable_turns >= 2:
                return self._summary(
                    engine._non_retryable_failure_message(turn_results),
                    engine._blocked_outcome(turn_results),
                )
        else:
            state.consecutive_non_retryable_turns = 0
        return None
