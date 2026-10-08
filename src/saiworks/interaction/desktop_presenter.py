from __future__ import annotations

from dataclasses import dataclass
import json
from threading import Event, Lock


def semantic_action_name(name: str) -> str:
    return {
        "shell_exec": "Run a local command",
        "patch": "Modify workspace files",
        "workspace_open": "Open another workspace",
        "read_file": "Read a workspace file",
        "open_application": "Open an application",
    }.get(name, name.replace("_", " ").title())


def format_approval_prompt(action, reason: str) -> str:
    arguments = json.dumps(action.arguments, ensure_ascii=False, indent=2, default=str)
    return (
        "SaiWorks Runtime is requesting permission to run an action.\n\n"
        f"Action: {semantic_action_name(action.name)}\n"
        f"Reason: {reason}\n\n"
        f"Arguments:\n{arguments}\n\n"
        "On macOS, a later system Automation prompt may name the app that launched "
        "SaiWorks (such as Zed, Terminal, or Python)."
    )


class DesktopApprovalRequest:
    """One shell-local approval rendezvous that can be safely resolved once."""

    def __init__(self, action, reason: str) -> None:
        self.action = action
        self.reason = reason
        self._completed = Event()
        self._lock = Lock()
        self._decision: bool | None = None

    @property
    def is_resolved(self) -> bool:
        return self._completed.is_set()

    def resolve(self, approved: bool) -> bool:
        with self._lock:
            if self._completed.is_set():
                return False
            self._decision = bool(approved)
            self._completed.set()
            return True

    def wait(self) -> bool:
        self._completed.wait()
        return self._decision is True


@dataclass(frozen=True, slots=True)
class ActivityStep:
    state: str
    label: str


class DesktopActivityModel:
    """Shell-local projection for the compact card and execution rail."""

    def __init__(self) -> None:
        self._steps: list[ActivityStep] = []
        self.headline = "SaiWorks is ready"

    @property
    def steps(self) -> tuple[ActivityStep, ...]:
        return tuple(self._steps)

    def start(self, label: str = "Understanding your request") -> None:
        self._steps = [ActivityStep("current", label)]
        self.headline = f"●  {label}"

    def update(self, label: str) -> None:
        label = label.rstrip("…")
        if self._steps and self._steps[-1].state == "current":
            previous = self._steps[-1]
            self._steps[-1] = ActivityStep("done", previous.label)
        if self._steps and self._steps[-1].label == label:
            self._steps[-1] = ActivityStep("current", label)
        else:
            self._steps.append(ActivityStep("current", label))
        self.headline = f"●  {label}"

    def finish(self, outcome: str) -> str:
        state, label, symbol = {
            "completed": ("done", "Completed and verified", "✓"),
            "interrupted": ("cancelled", "Cancelled and stopped", "■"),
            "blocked": ("blocked", "Blocked: needs your input", "!"),
            "exhausted": ("warning", "Execution limit reached", "!"),
            "stalled": ("warning", "Stopped making progress", "!"),
            "runtime_error": ("failed", "Runtime error", "!"),
            "failed": ("failed", "Failed", "!"),
        }.get(outcome, ("warning", f"Finished: {outcome.replace('_', ' ')}", "!"))
        if self._steps and self._steps[-1].state == "current":
            previous = self._steps[-1]
            self._steps[-1] = ActivityStep(state, previous.label)
        self._steps.append(ActivityStep(state, label))
        self.headline = f"{symbol}  {label}"
        return label

    def reset(self) -> None:
        self._steps = []
        self.headline = "SaiWorks is ready"


class DesktopProgressReporter:
    """Translate engine progress into presentation-safe Desktop status text."""

    def __init__(self, publish) -> None:
        self.publish = publish

    def model_started(self, message: str = "Model is thinking…"):
        self.publish(message)
        return "model"

    def model_finished(self, _handle) -> None:
        self.publish("Preparing result…")

    def model_stream_delta(self, _handle, _channel: str, _text: str) -> None:
        return None

    def model_retrying(self, _handle, retry: int, max_retries: int, status: str, delay_seconds: float) -> None:
        self.publish(f"Model retry {retry}/{max_retries}: {status}; waiting {delay_seconds:g}s")

    def tool_started(self, action_name: str):
        self.publish(f"{semantic_action_name(action_name)}…")
        return action_name

    def tool_finished(self, _handle, action, result) -> None:
        state = "completed" if result.success else "failed"
        self.publish(f"{semantic_action_name(action.name)} {state}")

    def tool_aborted(self, handle) -> None:
        self.publish(f"{handle} interrupted")

    def tool_skipped(self, action, reason: str) -> None:
        self.publish(f"{semantic_action_name(action.name)} skipped: {reason}")
