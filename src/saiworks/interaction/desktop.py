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
