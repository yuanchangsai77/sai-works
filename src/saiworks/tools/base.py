from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from enum import Enum

from ..types import ToolAction, ToolDefinition, ToolResult


class Tool(Protocol):
    name: str
    description: str
    arguments: dict[str, str]
    input_schema: dict
    risk_level: str
    evidence_kinds: list[str]
    exposed: bool

    def run(self, action: ToolAction, context: ToolContext) -> ToolResult:
        """Execute a structured tool action."""

    def definition(self) -> ToolDefinition:
        """Return the normalized tool definition."""

    def summarize(self, result: ToolResult) -> str:
        """Return a user-facing run summary for a result."""


class ExecutionPhase(Enum):
    UNKNOWN = "unknown"
    NOT_STARTED = "not_started"
    STARTED = "started"


@dataclass(frozen=True, slots=True)
class ToolExecution:
    result: ToolResult
    phase: ExecutionPhase = ExecutionPhase.UNKNOWN


@dataclass(slots=True)
class EffectTracker:
    _started: bool = field(default=False, init=False)

    @property
    def started(self) -> bool:
        return self._started

    def begin(self) -> None:
        self._started = True


@dataclass(slots=True)
class ToolContext:
    cwd: str
    state: dict = field(default_factory=dict)
    allowed_roots: list[str] = field(default_factory=list)
    max_output_bytes: int = 32_000
    effect_tracker: EffectTracker | None = None
    resource_guard: Callable | None = None


@dataclass(slots=True)
class SimpleTool:
    name: str
    description: str
    arguments: dict[str, str]
    handler: Callable[[ToolAction, ToolContext], ToolResult]
    input_schema: dict = field(default_factory=dict)
    risk_level: str = "read"
    evidence_kinds: list[str] = field(default_factory=list)
    exposed: bool = True
    tracks_effects: bool = False
    # User-facing run summary only. It is local display logic and must not be
    # written into ToolResult output or metadata.
    summarizer: Callable[[ToolResult], str] | None = None
    observation_adapter: Callable | None = None
    observation_valid: Callable | None = None
    # Internal opt-in: handler may run with isolated read-only batch state.
    parallel_read: bool = False

    def run(self, action: ToolAction, context: ToolContext) -> ToolResult:
        return self.handler(action, context)

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            arguments=dict(self.arguments),
            input_schema=dict(self.input_schema),
            risk_level=self.risk_level,
            evidence_kinds=list(self.evidence_kinds),
        )

    def summarize(self, result: ToolResult) -> str:
        if self.summarizer is None:
            return result.output
        return self.summarizer(result)
