from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..intent import RequestIntent
from ..observations import observation_for
from ..types import ToolAction, ToolResult


class ProgressReporter(Protocol):
    """Optional execution progress sink.

    ExecutionEngine emits these events without depending on a terminal UI.
    Implementations can render a TUI, collect telemetry, or ignore events.
    """

    def model_started(self, message: str = "Model is thinking…") -> Any:
        ...


    def model_finished(self, handle: Any) -> None:
        ...

    def model_stream_delta(self, handle: Any, channel: str, text: str) -> None:
        """Render a presentation-safe natural-language model delta."""
        ...

    def model_retrying(
        self,
        handle: Any,
        retry: int,
        max_retries: int,
        status: str,
        delay_seconds: float,
    ) -> None:
        ...

    def tool_started(self, action_name: str) -> Any:
        ...

    def tool_finished(self, handle: Any, action: ToolAction, result: ToolResult) -> None:
        ...

    def tool_aborted(self, handle: Any) -> None:
        ...

    def tool_skipped(self, action: ToolAction, reason: str) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ProgressContext:
    intent: RequestIntent
    results: list[ToolResult]
    recovery_sent: bool
    previous_results: list[ToolResult] = field(default_factory=list)
    visible_observations: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class ProgressSignal:
    repeated_actions: list[dict[str, object]]


class ProgressPolicy(Protocol):
    def evaluate(self, context: ProgressContext) -> ProgressSignal | None:
        ...


class DefaultProgressPolicy:
    def evaluate(self, context: ProgressContext) -> ProgressSignal | None:
        if self.has_new_observation(context.results, context.previous_results):
            return None
        records = [observation_for(result) for result in context.results if result.success]
        records = [record for record in records if record is None or not record.control]
        if not records or any(record is None or not record.certain or not record.complete for record in records):
            return None
        previous = [observation_for(result) for result in context.previous_results if result.success
                    and result.metadata.get("_observation_body_complete", True)]
        previous = [record.evidence_key for record in previous if record is not None
                    and record.certain and record.complete and not record.control
                    and (context.visible_observations is None or record.identifier in context.visible_observations)]
        threshold = 1 if context.intent.file_changes and not context.recovery_sent else 2
        if all(previous.count(record.evidence_key) >= threshold for record in records):
            return ProgressSignal(repeated_actions=[result.metadata.get("action_arguments", {}) for result in context.results])
        return None

    @classmethod
    def has_new_observation(cls, results: list[ToolResult], previous_results: list[ToolResult]) -> bool:
        previous = [observation_for(result) for result in previous_results if result.success]
        known = {record.evidence_key for record in previous if record is not None and record.certain}
        bodies = {(record.tool, record.workspace, record.body_hash) for record in previous if record is not None}
        for result in results:
            record = observation_for(result)
            if not result.success or result.metadata.get("duplicate") or record is None or record.control:
                continue
            if "workspace_change" in result.metadata.get("evidence", []):
                return True
            if record.certain and record.evidence_key not in known:
                return True
            if not record.certain and (record.tool, record.workspace, record.body_hash) not in bodies:
                return True
        return False
