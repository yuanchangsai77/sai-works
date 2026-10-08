from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..types import ExecutionSummary, StoredSession


@dataclass(frozen=True, slots=True)
class DesktopSessionView:
    """Immutable application state for a future Desktop renderer."""

    session_id: str
    cwd: str
    status: str
    messages: tuple[tuple[str, str], ...]
    context_generation: int


@dataclass(frozen=True, slots=True)
class DesktopTaskItem:
    session_id: str
    title: str
    status: str
    updated_at: str
    cwd: str


@dataclass(frozen=True, slots=True)
class DesktopDiagnosticView:
    session_id: str
    run_id: str
    started_at: str
    completed_at: str
    outcome: str
    event_count: int
    turn_count: int
    verification_phase: str
    open_issue: str
    recovery_hint: str
    tool_names: tuple[str, ...]
    log_path: str


class DesktopController:
    """GUI-independent Desktop shell adapter over the Runtime facade.

    The controller owns only shell-local selection state. Execution, session
    persistence, cancellation, recovery, and terminal outcomes remain Runtime
    responsibilities.
    """

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._session: StoredSession | None = None
        self._conversation: list[dict[str, str]] = []

    def open_workspace(self, cwd: str | Path, *, session_id: str | None = None) -> DesktopSessionView:
        if self.runtime.is_closed:
            raise RuntimeError("Runtime is closed")
        self._close_current_session()
        conversation: list[dict[str, str]] = []
        session = self.runtime.open_session(str(Path(cwd).resolve()), conversation, session_id)
        if session is None:
            raise RuntimeError("Desktop requires a configured session store")
        self.runtime.prepare_session_runtime(session)
        self._session = session
        self._conversation = conversation
        return self.view()

    def resume_latest(self) -> DesktopSessionView:
        latest = self.runtime.latest_session()
        if latest is None:
            raise RuntimeError("No saved session is available")
        return self.open_workspace(latest.cwd, session_id=latest.session_id)

    def resume(self, session_id: str) -> DesktopSessionView:
        stored = self.runtime.load_session(session_id)
        if stored is None:
            raise RuntimeError(f"Saved task '{session_id}' is no longer available")
        return self.open_workspace(stored.cwd, session_id=stored.session_id)

    def list_tasks(self, *, cwd: str | Path | None = None) -> tuple[DesktopTaskItem, ...]:
        target = str(Path(cwd).resolve()) if cwd is not None else None
        items = []
        for record in self.runtime.list_sessions():
            if target is not None and str(Path(record.cwd).resolve()) != target:
                continue
            title = self._task_title(record.preview)
            items.append(DesktopTaskItem(
                session_id=record.session_id,
                title=title[:64],
                status=record.status,
                updated_at=record.updated_at,
                cwd=record.cwd,
            ))
        return tuple(items)

    @staticmethod
    def _task_title(preview: str) -> str:
        text = preview.strip()
        if "User request:" in text:
            text = text.split("User request:", 1)[1]
        return " ".join(text.split()).strip()[:64] or "Untitled task"

    def diagnostics(self) -> DesktopDiagnosticView:
        session = self._require_session()
        latest_trace = session.trace[-1] if session.trace else None
        resume = session.resume_state
        run_id = latest_trace.run_id if latest_trace is not None else resume.last_run_id
        log_path = self.runtime.run_log_path(run_id)
        return DesktopDiagnosticView(
            session_id=session.session_id,
            run_id=run_id,
            started_at=(latest_trace.started_at if latest_trace is not None else ""),
            completed_at=(latest_trace.completed_at if latest_trace is not None else ""),
            outcome=(latest_trace.outcome if latest_trace is not None else resume.last_outcome),
            event_count=(latest_trace.event_count if latest_trace is not None else 0),
            turn_count=(latest_trace.turn_count if latest_trace is not None else 0),
            verification_phase=str(
                latest_trace.checkpoint.phase if latest_trace is not None else resume.checkpoint.phase
            ),
            open_issue=resume.open_issue,
            recovery_hint=resume.recovery_hint,
            tool_names=tuple(latest_trace.tool_names if latest_trace is not None else resume.last_tool_names),
            log_path=str(log_path) if log_path is not None else "",
        )

    def submit(
        self,
        prompt: str,
        *,
        context_paths: Iterable[str] = (),
    ) -> ExecutionSummary | None:
        session = self._require_session()
        request = self.runtime.build_session_request(
            prompt,
            session.cwd,
            session,
            self._conversation,
            list(context_paths),
        )
        generation = session.context_generation
        if not self.runtime.prepare_turn(session, request, generation, self._conversation):
            return None
        summary = self.runtime.execute_session_turn(
            request,
            prompt,
            session,
            self._conversation,
            generation,
        )
        if summary is None:
            self.runtime.discard_prepared_turn(request)
        return summary

    def cancel_current(self) -> None:
        self.runtime.cancel_current_run()

    def close(self, *, force: bool = False) -> None:
        self._close_current_session()
        self.runtime.close(force=force)

    def _close_current_session(self) -> None:
        if self._session is None:
            return
        self.runtime.close_session(self._session, self._conversation)
        self._session = None
        self._conversation = []

    def view(self) -> DesktopSessionView:
        session = self._require_session()
        messages = tuple(
            (str(item.get("role", "")), str(item.get("content", "")))
            for item in session.messages
        )
        return DesktopSessionView(
            session_id=session.session_id,
            cwd=session.cwd,
            status=session.status,
            messages=messages,
            context_generation=session.context_generation,
        )

    def _require_session(self) -> StoredSession:
        if self._session is None:
            raise RuntimeError("Open a workspace before using the Desktop controller")
        return self._session
