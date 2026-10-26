from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..types import ExecutionSummary, ToolResult


@dataclass(slots=True)
class RunState:
    """Mutable facts that belong to one engine invocation, not its Task binding."""

    started_at: float = field(default_factory=time.monotonic)
    model_attempts: int = 0
    consecutive_model_timeouts: int = 0
    consecutive_non_retryable_turns: int = 0
    consecutive_failed_test_turns: int = 0
    invalid_completion_count: int = 0
    progress_recovery_sent: bool = False
    approved_actions: set[tuple[str, str, str]] = field(default_factory=set)
    completed_actions: dict[str, ToolResult] = field(default_factory=dict)
    duplicate_counts: dict[str, int] = field(default_factory=dict)
    approval_wait_seconds: float = 0.0
    tools_closed: bool = False
    terminal_summary: ExecutionSummary | None = None

    def clear_action_cache(self) -> None:
        self.completed_actions.clear()
        self.duplicate_counts.clear()
