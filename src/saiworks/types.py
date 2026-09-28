from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar


@dataclass(slots=True)
class UserRequest:
    prompt: str
    cwd: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolDefinition:
    """Model-visible tool contract used for prompting, API tool schema, and policy checks."""

    name: str
    description: str
    arguments: dict[str, str] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "read"
    evidence_kinds: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolAction:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ModelReply:
    message: str
    actions: list[ToolAction] = field(default_factory=list)
    done: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    """Tool execution result.

    `output` is model-visible session history. Ordinary `metadata` is structured
    runtime/log/test data and is not prompt-visible, except keys explicitly copied
    by the session layer such as `action_arguments`.
    """

    name: str
    success: bool
    output: str
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RuntimeBlocker:
    """Runtime-owned reason why a task cannot currently complete."""

    error_code: str
    summary: str
    source: str = "runtime"
    tool: str = ""
    retryability: str = "conditional"
    required_action: str = "resume"


@dataclass(slots=True)
class EvidenceRecord:
    """Runtime-owned proof tied to one task and workspace revision."""

    kind: str
    producer: str
    task_id: str
    workspace_revision: int
    artifact_refs: list[str] = field(default_factory=list)
    source_task_ids: list[str] = field(default_factory=list)


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    VERIFIED = "verified"
    DONE = "done"


@dataclass(slots=True)
class TaskCheckpoint:
    """Bounded projection of execution facts used for recovery and handoff."""

    objective: str = ""
    schema_version: int = 3
    task_id: str = ""
    workspace_root: str = ""
    workspace_revision: int = 0
    phase: TaskStatus | str = TaskStatus.PENDING
    completed_actions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    evidence: list[EvidenceRecord] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    unmet_deliverables: list[str] = field(default_factory=list)
    blockers: list[RuntimeBlocker] = field(default_factory=list)
    runtime_state: dict[str, str] = field(default_factory=dict)

    _PHASE_ALIASES: ClassVar[dict[str, TaskStatus]] = {
        "executing": TaskStatus.IN_PROGRESS,
        "incomplete": TaskStatus.IN_PROGRESS,
        "completed": TaskStatus.DONE,
    }
    _PHASE_TRANSITIONS: ClassVar[dict[TaskStatus, set[TaskStatus]]] = {
        TaskStatus.PENDING: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
        TaskStatus.IN_PROGRESS: {TaskStatus.BLOCKED, TaskStatus.VERIFIED},
        TaskStatus.BLOCKED: {TaskStatus.IN_PROGRESS},
        TaskStatus.VERIFIED: {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
        TaskStatus.DONE: {TaskStatus.IN_PROGRESS},
    }

    def __post_init__(self) -> None:
        self.schema_version = max(3, self.schema_version) if isinstance(self.schema_version, int) else 3
        try:
            self.phase = self._coerce_phase(self.phase)
        except ValueError:
            self.phase = TaskStatus.PENDING

    @classmethod
    def _coerce_phase(cls, phase: str | TaskStatus) -> TaskStatus:
        if isinstance(phase, TaskStatus):
            return phase
        if not isinstance(phase, str):
            raise ValueError(f"Unsupported task state: {phase!r}")
        if phase in cls._PHASE_ALIASES:
            return cls._PHASE_ALIASES[phase]
        try:
            return TaskStatus(phase)
        except ValueError as error:
            raise ValueError(f"Unsupported task state: {phase!r}") from error

    def transition_to(self, phase: str | TaskStatus) -> None:
        """Move the task through its canonical lifecycle, rejecting invalid transitions."""
        phase = self._coerce_phase(phase)
        if phase == self.phase:
            return
        if phase not in self._PHASE_TRANSITIONS.get(self.phase, set()):
            raise ValueError(f"Invalid task state transition: {self.phase} -> {phase}")
        self.phase = phase

    def mark_done(self) -> None:
        if self.phase == TaskStatus.PENDING:
            self.transition_to(TaskStatus.IN_PROGRESS)
        if self.phase == TaskStatus.IN_PROGRESS:
            self.transition_to(TaskStatus.VERIFIED)
        self.transition_to(TaskStatus.DONE)

    def revoke_completion(self) -> None:
        if self.phase in {TaskStatus.VERIFIED, TaskStatus.DONE}:
            self.transition_to(TaskStatus.IN_PROGRESS)


@dataclass(slots=True)
class ResourceDescriptor:
    id: str
    name: str
    description: str = ""
    source: str = ""
    mime_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ResourceContent:
    id: str
    text: str
    mime_type: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .capabilities.model import InstructionContent


@dataclass(slots=True)
class ExecutionSummary:
    final_message: str
    tool_results: list[ToolResult]
    active_instructions: list[InstructionContent] = field(default_factory=list)
    active_capability_ids: list[str] = field(default_factory=list)
    outcome: str = "completed"
    blockers: list[RuntimeBlocker] = field(default_factory=list)
    checkpoint: TaskCheckpoint = field(default_factory=TaskCheckpoint)
    workspace_state: "WorkspaceSessionState" = field(default_factory=lambda: WorkspaceSessionState())



@dataclass(slots=True)
class SessionRecord:
    session_id: str
    cwd: str
    created_at: str
    updated_at: str
    status: str
    message_count: int
    preview: str


@dataclass(slots=True)
class SessionTurnTrace:
    turn: int
    message: str
    actions: list[str] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)
    action_details: list[str] = field(default_factory=list)
    tool_result_details: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionRunTrace:
    run_id: str
    started_at: str
    completed_at: str
    prompt: str
    final_message: str
    outcome: str
    event_count: int
    turn_count: int
    tool_names: list[str] = field(default_factory=list)
    turns: list[SessionTurnTrace] = field(default_factory=list)
    blockers: list[RuntimeBlocker] = field(default_factory=list)
    checkpoint: TaskCheckpoint = field(default_factory=TaskCheckpoint)


@dataclass(slots=True)
class SessionResumeState:
    last_run_id: str = ""
    last_user_prompt: str = ""
    last_assistant_message: str = ""
    last_outcome: str = ""
    last_tool_names: list[str] = field(default_factory=list)
    open_issue: str = ""
    recovery_hint: str = ""
    blockers: list[RuntimeBlocker] = field(default_factory=list)
    checkpoint: TaskCheckpoint = field(default_factory=TaskCheckpoint)


@dataclass(slots=True)
class WorkspaceSessionState:
    """Persistent workspace authority owned by one user session."""

    origin_root: str = ""
    active_root: str = ""
    approved_roots: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StoredSession:
    session_id: str
    cwd: str
    created_at: str
    updated_at: str
    status: str
    messages: list[dict[str, str]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    active_capability_ids: list[str] = field(default_factory=list)
    trace: list[SessionRunTrace] = field(default_factory=list)
    resume_state: SessionResumeState = field(default_factory=SessionResumeState)
    workspace_state: WorkspaceSessionState = field(default_factory=WorkspaceSessionState)
    parent_session_id: str = ""
    cluster_id: str = ""
    session_role: str = "primary"
    launch_source: str = "direct"
    session_image_id: str = ""
    revision: int = 0
