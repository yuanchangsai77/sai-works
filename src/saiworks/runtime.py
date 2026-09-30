from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from pathlib import Path
from threading import Condition
from uuid import uuid4

from .orchestration.engine import ExecutionContext
from .context.packager import ContextBudgetExceededError
from .sessions.tasks import TaskStore
from .sessions.coordinator import SessionCoordinator
from .capabilities.runtime import RuntimeCapabilities
from .types import ExecutionSummary, SessionRecord, StoredSession, UserRequest, WorkspaceSessionState


class Runtime:
    """Headless run/session coordination; one FIFO execution slot per instance.

    Legacy session formats and synchronous approval remain compatible.
    Accepted submissions are durable; detached approvals are a later phase.
    """

    def __init__(self, engine, logger=None, session_store=None,
                 subagent_coordinator=None, subagent_runner=None, subagent_grant=None, task_store=None):
        self.engine = engine
        self.logger = logger
        self.session_store = session_store
        self.subagent_coordinator = subagent_coordinator
        self.subagent_runner = subagent_runner
        self.subagent_grant = subagent_grant
        self.task_store = task_store
        self.sessions = SessionCoordinator(self)
        self.capabilities = RuntimeCapabilities(self)
        self.execution_factory = None
        self.on_tools_changed = None
        self.on_run_started = None
        self.on_run_finished = None
        self.on_run_interrupted = None
        self._active_task_id = None
        self._pending_submission = None
        self._active_context = None
        self._task_contexts = {}
        self.last_summary = None
        self._condition = Condition()
        self._queue = deque()
        self._running = False

    @contextmanager
    def execution_slot(self, ticket=None):
        with self._condition:
            if ticket is None:
                ticket = object()
                self._queue.append(ticket)
                self._condition.notify_all()
            try:
                self._condition.wait_for(lambda: not self._running and self._queue[0] is ticket)
            except BaseException:
                self._queue.remove(ticket)
                self._condition.notify_all()
                raise
            self._queue.popleft()
            self._running = True
        try:
            yield
        finally:
            with self._condition:
                self._running = False
                self._condition.notify_all()

    def accept(self, request: UserRequest, *, submission_id: str):
        if self.task_store is None:
            raise RuntimeError("Durable acceptance requires a task store")
        if self.subagent_grant is not None:
            self._validate_subagent_grant(request)
        checkpoint_factory = getattr(self.engine, "_initial_checkpoint", None)
        task_id = checkpoint_factory(request).task_id if callable(checkpoint_factory) else None
        record = self.task_store.accept(request, submission_id=submission_id, task_id=task_id)
        with self._condition:
            self._task_contexts.setdefault(record.task_id, ExecutionContext(task_id=record.task_id))
        return record

    def get_submission(self, submission_id: str):
        return self.task_store.get_submission(submission_id) if self.task_store is not None else None

    def execute(self, request: UserRequest, *, background: bool = False,
                submission_id: str | None = None,
                _session: StoredSession | None = None) -> ExecutionSummary:
        with self._condition:
            record = self.accept(request, submission_id=submission_id if submission_id is not None else uuid4().hex) if self.task_store is not None else None
            ticket = object()
            self._queue.append(ticket)
            self._condition.notify_all()
        with self.execution_slot(ticket):
            if self._pending_submission is not None:
                raise RuntimeError("Previous delegated result is awaiting validation and persistence")
            if record is None:
                return self._execute(request, background=background, session=_session)
            current = self.task_store.get_submission(record.submission_id)
            if current.result is not None:
                self._task_contexts.pop(record.task_id, None)
                return current.result
            if not self.task_store.claim(record.submission_id):
                raise RuntimeError("Accepted task is already running or needs explicit recovery; it will not be replayed")
            bound_request = record.request()
            context = self._task_contexts[record.task_id]
            self.last_summary = None
            self.engine.last_failure_summary = None
            try:
                self._active_context = context
                self._prepare_task(record.task_id)

                summary = self._execute(bound_request, background=background, context=context, session=_session)
            except BaseException as error:
                partial = self.last_summary or getattr(self.engine, "last_failure_summary", None)
                if background and bound_request.metadata.get("defer_finalize"):
                    self._pending_submission = record.submission_id
                    raise
                self.task_store.finish(record.submission_id, partial,
                                       run_id=getattr(self.logger, "last_run_id", None),
                                       error=type(error).__name__)
                raise
            else:
                if background and bound_request.metadata.get("defer_finalize"):
                    self._pending_submission = record.submission_id
                    return summary
                self.task_store.finish(record.submission_id, summary,
                                       run_id=getattr(self.logger, "last_run_id", None))
                return summary
            finally:
                self._active_context = None
                if self._pending_submission != record.submission_id:
                    self._task_contexts.pop(record.task_id, None)
                if "last_session_user_message" in bound_request.metadata:
                    request.metadata["last_session_user_message"] = bound_request.metadata["last_session_user_message"]

    def _notify(self, name: str, value) -> None:
        callback = getattr(self, name)
        if callback is None:
            return
        try:
            callback(value)
        except Exception as error:
            if self.logger is not None:
                self.logger.record("observer.error", {"observer": name, "error": type(error).__name__})

    def _prepare_task(self, task_id: str) -> None:
        if self._active_task_id == task_id:
            return
        self.last_summary = None
        if self.execution_factory is not None:
            self.close_tool_state()
            assembled = self.execution_factory()
            self.engine.tools = assembled.tools
            self.engine.capability_warehouse = assembled.capability_warehouse
            self.engine.resource_providers = assembled.resource_providers
            self.engine._tool_state_session_key = None
            self._notify("on_tools_changed", assembled.tools)
        self._active_task_id = task_id

    def cancel_current_run(self) -> None:
        context = self._active_context
        if context is not None:
            context.cancellation.set()
            if getattr(self.engine, "_execution_context", None) is context:
                self.engine.cancel_current_run()
        else:
            cancel = getattr(self.engine, "cancel_current_run", None)
            if callable(cancel):
                cancel()

    def run_background(self, request: UserRequest) -> ExecutionSummary:
        return self.execute(request, background=True)

    def _execute(self, request: UserRequest, *, background: bool, context: ExecutionContext | None = None, session=None) -> ExecutionSummary:
        if background and self.subagent_grant is not None:
            self._validate_subagent_grant(request)
        self.last_summary = None
        if self.logger is not None:
            registered_skills = []
            if not background:
                for loader in getattr(self.engine, "context_loaders", []):
                    if hasattr(loader, "registry"):
                        registered_skills = sorted(loader.registry._skills.keys())
                        break
            self.logger.start_run(request, registered_skills=registered_skills)
            if session is not None and self.session_store is not None:
                self._attach_last_run_id(session)
                self.session_store.save(session)
        if not background:
            self._notify("on_run_started", request)
        try:
            execute_in_context = getattr(self.engine, "execute_in_context", None)
            summary = execute_in_context(request, context) if context is not None and callable(execute_in_context) else self.engine.execute(request)
        except KeyboardInterrupt:
            summary = getattr(self.engine, "last_failure_summary", None)
            if not isinstance(summary, ExecutionSummary) or summary.outcome != "interrupted":
                session = getattr(self.engine, "current_session", None)
                summary = ExecutionSummary(
                    final_message="Interrupted",
                    tool_results=list(getattr(session, "tool_results", [])),
                    outcome="interrupted",
                )
                cancel = getattr(self.engine, "cancel_current_run", None)
                if callable(cancel):
                    cancel()
                else:
                    self.close_tool_state()
                finish = getattr(self.engine, "_finish", None)
                if callable(finish):
                    finish(summary)
            self.last_summary = summary
            if self.logger is not None:
                self.logger.record("run.interrupted", {"tool_count": len(summary.tool_results)})
                self.logger.finalize(request, summary)
            if not background:
                self._notify("on_run_interrupted", summary)
            raise
        except Exception as error:
            if background:
                if self.logger is not None:
                    self.logger.record("run.error", {"message": "subagent execution failed"})
                raise
            if not isinstance(error, RuntimeError):
                if self.logger is not None:
                    partial = getattr(self.engine, "last_failure_summary", None)
                    if isinstance(partial, ExecutionSummary):
                        self.logger.finalize(request, partial)
                raise
            partial = getattr(self.engine, "last_failure_summary", None)
            if isinstance(error, ContextBudgetExceededError):
                summary = ExecutionSummary(final_message=str(error), tool_results=[], outcome="runtime_error")
            elif isinstance(partial, ExecutionSummary):
                summary = partial
            else:
                summary = ExecutionSummary(
                    final_message=f"Model API is unavailable right now. {error}. You can keep this session open and try again later.",
                    tool_results=[], outcome="runtime_error",
                )
            if self.logger is not None:
                self.logger.record("run.error", {"message": str(error)})
        self.last_summary = summary
        if self.logger is not None and not (background and request.metadata.get("defer_finalize")):
            self.logger.finalize(request, summary)
        if not background:
            self._notify("on_run_finished", summary)
        return summary

    def close_tool_state(self) -> None:
        return self.sessions.close_tool_state()

    def close_session(self, session, conversation) -> None:
        return self.sessions.close_session(session, conversation)

    def _validate_subagent_grant(self, request: UserRequest) -> None:
        grant = self.subagent_grant
        if not grant.is_runner_issued():
            raise RuntimeError("delegated subagent execution grant was not issued by the runner")
        subagent = request.metadata.get("subagent")
        if not isinstance(subagent, dict):
            raise RuntimeError("delegated subagent request is missing its execution identity")
        expected = {
            "session_id": request.metadata.get("session_id"),
            "cluster_id": subagent.get("cluster_id"),
            "parent_session_id": subagent.get("parent_session_id"),
            "attempt": subagent.get("attempt"),
            "workspace_root": str(Path(request.cwd).resolve()),
        }
        actual = {
            "session_id": grant.session_id,
            "cluster_id": grant.cluster_id,
            "parent_session_id": grant.parent_session_id,
            "attempt": grant.attempt,
            "workspace_root": str(Path(grant.workspace_root).resolve()),
        }
        if expected != actual:
            raise RuntimeError("delegated subagent request does not match its execution grant")
        contract = request.metadata.get("delegated_task")
        if not isinstance(contract, dict):
            raise RuntimeError("delegated subagent request is missing its task contract")
        if frozenset(contract.get("allowed_effects", [])) != grant.allowed_effects:
            raise RuntimeError("delegated subagent effects do not match the execution grant")
        if tuple(contract.get("allowed_resources", [])) != grant.allowed_resources:
            raise RuntimeError("delegated subagent resources do not match the execution grant")
        if grant.task_id and contract.get("task_id") != grant.task_id:
            raise RuntimeError("delegated subagent task id does not match the execution grant")
        if grant.objective and (
            request.prompt != grant.objective or contract.get("objective") != grant.objective
        ):
            raise RuntimeError("delegated subagent objective does not match the execution grant")
        if grant.required_evidence and tuple(contract.get("required_evidence", [])) != grant.required_evidence:
            raise RuntimeError("delegated subagent evidence contract does not match the execution grant")
        if grant.approval_policy and contract.get("approval_policy") != grant.approval_policy:
            raise RuntimeError("delegated subagent approval policy does not match the execution grant")

    def list_sessions(self) -> list[SessionRecord]:
        return self.sessions.list_sessions()

    def load_session(self, session_id: str) -> StoredSession | None:
        return self.sessions.load_session(session_id)

    def prepare_session_runtime(self, session: StoredSession | None) -> None:
        return self.sessions.prepare_session_runtime(session)

    def latest_session(self) -> StoredSession | None:
        return self.sessions.latest_session()

    def persist_run(
        self,
        session: StoredSession,
        prompt: str,
        summary: ExecutionSummary,
        *,
        model_user_content: str | None = None,
        status: str = "active",
        close_runtime: bool = False,
    ) -> None:
        if self._pending_submission is not None and self.task_store is not None:
            self.task_store.finish(self._pending_submission, summary,
                                   run_id=getattr(self.logger, "last_run_id", None))
            record = self.task_store.get_submission(self._pending_submission)
            self._task_contexts.pop(record.task_id, None)
            self._pending_submission = None
        return self.sessions.persist_run(session, prompt, summary, model_user_content=model_user_content, status=status, close_runtime=close_runtime)

    _apply_workspace_summary = staticmethod(SessionCoordinator._apply_workspace_summary)

    _workspace_root = staticmethod(SessionCoordinator._workspace_root)

    _ensure_workspace_state = staticmethod(SessionCoordinator._ensure_workspace_state)

    _cluster_state_for_outcome = staticmethod(SessionCoordinator._cluster_state_for_outcome)

    def _attach_last_run_id(self, session: StoredSession) -> None:
        return self.sessions._attach_last_run_id(session)

    def append_turn_and_persist(
        self,
        request: UserRequest,
        prompt: str,
        summary: ExecutionSummary,
        session: StoredSession | None,
        conversation: list[dict[str, str]],
        context_generation: int,
    ) -> None:
        return self.sessions.append_turn_and_persist(request, prompt, summary, session, conversation, context_generation)

    def execute_session_turn(
        self,
        request: UserRequest,
        prompt: str,
        session: StoredSession | None,
        conversation: list[dict[str, str]],
        context_generation: int,
    ) -> ExecutionSummary | None:
        return self.sessions.execute_session_turn(request, prompt, session, conversation, context_generation)

    def compaction_needed(self, conversation, pending_content: str) -> bool:
        return self.sessions.compaction_needed(conversation, pending_content)

    def compact_session(self, session, conversation):
        return self.sessions.compact_session(session, conversation)

    def open_session(self, cwd, conversation, session_id=None):
        return self.sessions.open_session(cwd, conversation, session_id)

    def prepare_turn(self, session, request, generation, conversation) -> bool:
        return self.sessions.prepare_turn(session, request, generation, conversation)

    def discard_prepared_turn(self, request) -> None:
        return self.sessions.discard_prepared_turn(request)

    def save_interrupted_session(self, session) -> None:
        return self.sessions.save_interrupted_session(session)

    def save_session(self, session) -> None:
        return self.sessions.save_session(session)

    def reset_session(self, session, conversation):
        return self.sessions.reset_session(session, conversation)

    def load_archived_messages(self, session_id, archive_id):
        return self.sessions.load_archived_messages(session_id, archive_id)

    def capability_command(self, args: list[str], session=None) -> dict:
        return self.capabilities.capability_command(args, session)

    def activate_skill(self, args: list[str], session=None) -> dict:
        return self.capabilities.activate_skill(args, session)

    def sync_session_capabilities(self, session, warehouse):
        return self.capabilities.sync_session_capabilities(session, warehouse)

    def safety_mode(self, requested=None):
        return self.capabilities.safety_mode(requested)

    def view(self):
        from .runtime_view import build_view
        return build_view(self.engine)

    def build_session_request(self, prompt, cwd, session, conversation, context_paths):
        return self.sessions.build_session_request(prompt, cwd, session, conversation, context_paths)

    def session_usage(self, session):
        return self.sessions.session_usage(session)
