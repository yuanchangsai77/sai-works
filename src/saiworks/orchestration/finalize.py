from __future__ import annotations

from ..types import ExecutionSummary


class RunFinalizer:
    """Commit one engine terminal projection; Runtime owns durable delivery."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def finish(self, summary: ExecutionSummary) -> ExecutionSummary:
        engine = self.engine
        state = engine._run_state
        if state.terminal_summary is not None:
            return state.terminal_summary
        if summary.outcome == "completed" and summary.tool_results:
            summary.outcome = engine._aggregate_outcome(summary.tool_results)
        if engine.current_session is not None:
            summary.checkpoint = engine.current_session.checkpoint
            summary.workspace_state = engine.current_session.workspace_state
        unresolved = engine._unresolved_results(summary.tool_results)
        if summary.outcome == "completed":
            summary.blockers = []
            summary.checkpoint.blockers = []
            summary.checkpoint.mark_done()
        else:
            summary.blockers = (
                list(summary.blockers)
                if summary.blockers and not unresolved
                else engine._runtime_blockers(unresolved, summary)
            )
            summary.checkpoint.blockers = list(summary.blockers)
            target_phase = "blocked" if summary.outcome == "blocked" else "in_progress"
            if summary.checkpoint.phase != target_phase:
                summary.checkpoint.transition_to(target_phase)
        engine.current_session = None
        if engine._keep_tool_state and engine.capability_warehouse is not None:
            summary.active_instructions = engine.capability_warehouse.persisted_instructions()
            summary.active_capability_ids = engine.capability_warehouse.persisted_capability_ids()
            engine.capability_warehouse.release_scopes({"turn", "run"})
        if not engine._keep_tool_state or engine._execution_context.cancellation.is_set():
            engine._close_execution_tools()
        state.terminal_summary = summary
        engine._execution_context.terminal_summary = summary
        try:
            engine.logger.record("run.metrics", {
                "turns": state.turn_count,
                "model_attempts": state.model_attempts,
                "real_tool_actions": state.real_tool_actions,
                "duplicate_skips": state.duplicate_skips,
                "invalid_completions": state.invalid_completion_count,
                "model_seconds": round(state.model_seconds, 3),
                "tool_seconds": round(state.tool_seconds, 3),
                "approval_wait_seconds": round(state.approval_wait_seconds, 3),
            })
        except Exception:
            # Diagnostics must not replace a committed run outcome.
            pass
        return summary
