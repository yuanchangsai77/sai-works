from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, RLock
from uuid import uuid4
from dataclasses import asdict

from .orchestration.engine import ExecutionContext
from .orchestration.action_journal import TaskActionJournal
from .orchestration.continuation import WaitRequested
from .observability.events import Event
from .context.packager import ContextBudgetExceededError
from .orchestration.failures import runtime_failure_message
from .sessions.tasks import ApprovalRecord, TaskSnapshot, TaskStore
from .sessions.coordinator import SessionCoordinator
from .runtime_observation import TaskAccess, TaskWatch
from .runtime_control import TaskControl
from .runtime_interactions import RuntimeInteractions
from .capabilities.runtime import RuntimeCapabilities
from .types import ExecutionSummary, SessionRecord, StoredSession, UserRequest, WorkspaceSessionState


class Runtime:
    """Headless run/session coordination; one FIFO execution slot per instance.

    Legacy session formats and synchronous approval remain compatible.
    Accepted submissions are durable; detached approvals are a later phase.
    """

    def __init__(self, engine, logger=None, session_store=None,
                 subagent_coordinator=None, subagent_runner=None, subagent_grant=None, task_store=None,
                 approval_ttl_seconds: int = 300):
        self.engine = engine
        self.logger = logger
        self.session_store = session_store
        self.subagent_coordinator = subagent_coordinator
        self.subagent_runner = subagent_runner
        self.subagent_grant = subagent_grant
        self.task_store = task_store
        self.approval_ttl_seconds = approval_ttl_seconds
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
        self._task_context_users = {}
        self.last_summary = None
        self._condition = Condition()
        self._queue = deque()
        self._running = False
        self._closed = False
        self._shutdown_lock = RLock()
        self._close_cleanup_complete = False
        self._task_access = {}
        self._task_control = {}
        self._delivery_lock = RLock()
        self._saved_deliveries = set()
        self.interactions = RuntimeInteractions(self)
        self.user_input_callback = None
        self._synchronous_waits = set()

    def grant_task_access(self, task_id: str) -> TaskAccess:
        """Trusted local owner entry; never expose capability issuance to clients."""
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            if self.task_store is None:
                raise RuntimeError("Task observation requires a task store")
            store = self.task_store
        store.snapshot(task_id)
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            access = TaskAccess()
            self._task_access[access] = task_id
            return access

    def revoke_task_access(self, access: TaskAccess) -> None:
        with self._condition:
            if type(access) is not TaskAccess:
                raise PermissionError("Invalid task observation access")
            self._task_access.pop(access, None)

    def snapshot(self, access: TaskAccess) -> TaskSnapshot:
        with self._condition:
            if self._closed or type(access) is not TaskAccess or access not in self._task_access:
                raise PermissionError("Task observation access is unavailable")
            task_id = self._task_access[access]
            store = self.task_store
        snapshot = store.snapshot(task_id)
        with self._condition:
            if self._closed or access not in self._task_access:
                raise PermissionError("Task observation access is unavailable")
        return snapshot

    def watch(self, access: TaskAccess, after_revision: int | None = None) -> TaskWatch:
        snapshot = self.snapshot(access)
        if after_revision is not None and (type(after_revision) is not int or not 0 <= after_revision <= snapshot.revision):
            raise ValueError("Invalid task revision")
        return TaskWatch(self, access, after_revision)

    def grant_task_control(self, task_id: str, permissions, *, actor: str = "local") -> TaskControl:
        """Trusted local owner entry; never expose control issuance to clients."""
        granted = frozenset(permissions)
        if not granted or granted - {"cancel", "approve", "reply"}:
            raise ValueError("Unknown task control permission")
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            if self.task_store is None:
                raise RuntimeError("Task control requires a task store")
            store = self.task_store
        store.snapshot(task_id)
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            control = TaskControl()
            self._task_control[control] = (task_id, granted, str(actor))
            return control

    def revoke_task_control(self, control: TaskControl) -> None:
        with self._condition:
            if type(control) is not TaskControl:
                raise PermissionError("Invalid task control")
            self._task_control.pop(control, None)

    def _require_control(self, control: TaskControl, permission: str) -> tuple[str, str]:
        with self._condition:
            if self._closed or type(control) is not TaskControl or control not in self._task_control:
                raise PermissionError("Task control is unavailable")
            task_id, permissions, actor = self._task_control[control]
        if permission not in permissions:
            raise PermissionError(f"Task control lacks {permission!r} permission")
        return task_id, actor

    def request_approval(
        self,
        submission_id: str,
        action_name: str,
        arguments: dict,
        *,
        required_scope: str,
        now=None,
    ) -> ApprovalRecord:
        """Owner-level approval creation; shells never create approval records."""
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            if self.task_store is None:
                raise RuntimeError("Approval records require a task store")
            store = self.task_store
        submission = store.get_submission(submission_id)
        if submission is None:
            raise KeyError(submission_id)
        return store.request_approval(
            task_id=submission.task_id,
            submission_id=submission_id,
            action_name=action_name,
            arguments=dict(arguments),
            workspace_root=submission.request().cwd,
            required_scope=required_scope,
            ttl_seconds=self.approval_ttl_seconds,
            now=now,
        )

    def decide(
        self,
        control: TaskControl,
        approval_id: str,
        decision: str,
        *,
        expected_action_fingerprint: str,
        now=None,
    ) -> ApprovalRecord:
        """Submit a human decision; the first committed ruling stands."""
        task_id, actor = self._require_control(control, "approve")
        if self.task_store is None:
            raise RuntimeError("Approval records require a task store")
        approval = self.task_store.get_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        if approval.task_id != task_id:
            raise PermissionError("Approval belongs to a different task")
        result = self.task_store.decide(
            approval_id,
            decision,
            expected_action_fingerprint=expected_action_fingerprint,
            actor=actor,
            now=now,
        )
        with self._condition:
            self._condition.notify_all()
        return result

    def reply(self, control: TaskControl, interaction_id: str, answer: str):
        task_id, _actor = self._require_control(control, 'reply')
        result = self.task_store.answer(task_id, interaction_id, answer)
        with self._condition:
            self._condition.notify_all()
        return result

    def advance(self, submission_id: str):
        """Advance an accepted submission to its next wait or terminal result."""
        record = self.get_submission(submission_id)
        if record is None:
            raise KeyError(submission_id)
        return self._execute_segment(record.request(), submission_id=submission_id, _record=record)

    def cancel(self, control: TaskControl, *, now=None) -> str:
        """Record the authoritative cancellation acceptance point for the Task.

        Recording acceptance does not mean execution has stopped; interrupting
        started actions and reconciling their results remain execution-layer
        responsibilities.
        """
        task_id, _actor = self._require_control(control, "cancel")
        if self.task_store is None:
            raise RuntimeError("Cancellation acceptance requires a task store")
        accepted_at = self.task_store.accept_cancellation(task_id, now=now)
        with self._condition:
            context = self._task_contexts.get(task_id)
            if context is not None:
                context.cancellation.set()
            active = context is not None and self._active_context is context
        if active and getattr(self.engine, "_execution_context", None) is context:
            try:
                cancel_bound = getattr(self.engine, "cancel_execution", None)
                if callable(cancel_bound):
                    cancel_bound(context)
                else:
                    self.engine.cancel_current_run()
            except Exception as error:
                if self.logger is not None:
                    try:
                        self.logger.record("tool.cleanup_failed", {"error": str(error)})
                    except Exception:
                        pass
        return accepted_at

    def pending_approvals(self, control: TaskControl, *, now=None) -> tuple[ApprovalRecord, ...]:
        task_id, _actor = self._require_control(control, "approve")
        if self.task_store is None:
            raise RuntimeError("Approval records require a task store")
        return self.task_store.pending_approvals(task_id, now=now)

    def _complete_delivery(self, submission_id: str) -> None:
        if self.task_store is not None and self.task_store.get_submission(submission_id) is not None:
            self.task_store.set_delivery_state(submission_id, "completed")

    def _deliver_submission(self, submission_id: str, save) -> bool:
        """Retry a failed marker without repeating a successful local save."""
        with self._delivery_lock:
            record = self.get_submission(submission_id)
            if record is not None and record.delivery_state == "completed":
                self._saved_deliveries.discard(submission_id)
                return False
            if submission_id not in self._saved_deliveries:
                save()
                self._saved_deliveries.add(submission_id)
            self._complete_delivery(submission_id)
            self._saved_deliveries.discard(submission_id)
            return True

    @contextmanager
    def execution_slot(self, ticket=None):
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            if ticket is None:
                ticket = object()
                self._queue.append(ticket)
                self._condition.notify_all()
            try:
                self._condition.wait_for(
                    lambda: self._closed or (not self._running and self._queue[0] is ticket)
                )
                if self._closed:
                    self._queue.remove(ticket)
                    self._condition.notify_all()
                    raise RuntimeError("Runtime is closed")
            except BaseException:
                if ticket in self._queue:
                    self._queue.remove(ticket)
                self._condition.notify_all()
                raise
            self._queue.popleft()
            self._running = True
        try:
            yield
        finally:
            cleanup = False
            with self._condition:
                self._running = False
                cleanup = self._closed
                self._condition.notify_all()
            if cleanup:
                try:
                    self._finish_close()
                except Exception as error:
                    # Shutdown diagnostics cannot replace an already committed run outcome.
                    if self.logger is not None:
                        try:
                            self.logger.record("tool.cleanup_failed", {"error": str(error)})
                        except Exception:
                            pass

    def accept(self, request: UserRequest, *, submission_id: str):
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
        if self.task_store is None:
            raise RuntimeError("Durable acceptance requires a task store")
        if self.subagent_grant is not None:
            self._validate_subagent_grant(request)
        checkpoint_factory = getattr(self.engine, "_initial_checkpoint", None)
        task_id = checkpoint_factory(request).task_id if callable(checkpoint_factory) else None
        return self.task_store.accept(request, submission_id=submission_id, task_id=task_id)

    def get_submission(self, submission_id: str):
        return self.task_store.get_submission(submission_id) if self.task_store is not None else None

    def run_log_path(self, run_id: str) -> Path | None:
        base_dir = getattr(self.logger, "base_dir", None)
        if not run_id or base_dir is None:
            return None
        root = Path(base_dir).resolve()
        candidate = (root / run_id).resolve()
        if not candidate.is_relative_to(root):
            return None
        return candidate

    def execute(self, request: UserRequest, *, background: bool = False,
                submission_id: str | None = None, _session=None,
                _defer_terminal_notification: bool = False) -> ExecutionSummary:
        return self.interactions.execute(request, background=background, submission_id=submission_id,
                                         _session=_session, _defer_terminal_notification=_defer_terminal_notification)


    def _execute_segment(self, request: UserRequest, *, background: bool = False,
                submission_id: str | None = None,
                _session: StoredSession | None = None,
                _defer_terminal_notification: bool = False, _record=None):
        with self._condition:
            if self._closed:
                raise RuntimeError("Runtime is closed")
            record = _record or (self.accept(request, submission_id=submission_id if submission_id is not None else uuid4().hex) if self.task_store is not None else None)
            if record is not None and self.subagent_grant is not None:
                self._validate_subagent_grant(record.request())
            if record is not None and record.state == 'running' and not self._running and not self._queue:
                raise RuntimeError('Accepted task is already running or needs explicit recovery; it will not be replayed')
            if record is not None and record.result is None and not self.task_store.can_advance(record.submission_id):
                return self.get_submission(record.submission_id)
            if record is not None:
                self._retain_task_context(record.task_id)
            ticket = object()
            self._queue.append(ticket)
            self._condition.notify_all()
        with self.execution_slot(ticket):
            if self._pending_submission is not None:
                if record is not None:
                    self._release_task_context(record.task_id)
                raise RuntimeError("Previous delegated result is awaiting validation and persistence")
            if record is None:
                try:
                    summary = self._execute(request, background=background, session=_session)
                except KeyboardInterrupt:
                    if not _defer_terminal_notification:
                        self._notify_terminal(self.last_summary, background=background)
                    raise
                if not _defer_terminal_notification:
                    self._notify_terminal(summary, background=background)
                return summary
            bound_request = request
            claimed = False
            saved = None
            try:
                current = self.task_store.get_submission(record.submission_id)
                _defer_terminal_notification = _defer_terminal_notification or current.delivery_state == 'pending'
                if _defer_terminal_notification or (background and request.metadata.get("defer_finalize")):
                    self.task_store.set_delivery_state(record.submission_id, "pending")
                if current.result is not None:
                    return current.result
                if current.state in {'finished', 'cancelled'}:
                    raise RuntimeError('Task finished without a saved result; it will not be replayed')
                recovery = self.task_store.observe_pending_file_actions(record.task_id)
                if not self.task_store.claim(record.submission_id):
                    return self.get_submission(record.submission_id)
                claimed = True
                self.last_summary = None
                self.engine.last_failure_summary = None
                accepted_request = record.request()
                # Runtime has already selected the authoritative task. Diagnostic
                # text must not make the engine classify "continue" as a new task.
                bound_request = UserRequest(
                    accepted_request.prompt, accepted_request.cwd,
                    {**accepted_request.metadata, "task_id": record.task_id},
                )
                context = self._task_contexts[record.task_id]
                context.execution_started = False
                context.action_journal = TaskActionJournal(
                    self.task_store, record.task_id, record.submission_id, observations=recovery,
                )
                saved = self.task_store.continuation(record.submission_id)
                context.continuation = saved['payload'] if saved else None
                if self.task_store.cancellation_accepted(record.task_id) is not None:
                    context.cancellation.set()
                if saved:
                    self.close_tool_state()
                if saved:
                    bound_request.metadata['active_capability_ids'] = saved['payload']['capability_ids']
                    resume_log = getattr(self.logger, 'resume_run', None)
                    if callable(resume_log) and saved['payload'].get('logger'):
                        resume_log(saved['payload']['logger'])
                self.interactions.bind(context, record, saved)
                self._active_context = context
                self._prepare_task(record.task_id)
                attach = getattr(getattr(self.engine, 'tools', None), 'attach_state', None)
                if callable(attach):
                    attach('request_user_input', context.interaction_handler)

                summary = self._execute(bound_request, background=background, context=context, session=_session)
            except WaitRequested as waiting:
                wait_event = Event('run.waiting', {'kind': waiting.kind})
                publish_log = getattr(self.logger, 'publish_event', None)
                if self.logger is not None:
                    checkpoint_log = getattr(self.logger, 'checkpoint_run', None)
                    if callable(checkpoint_log):
                        waiting.continuation['logger'] = checkpoint_log()
                        if callable(publish_log):
                            waiting.continuation['logger']['events'].append(asdict(wait_event))
                try:
                    self.task_store.suspend(record.submission_id, waiting, ttl_seconds=self.approval_ttl_seconds)
                    if self.logger is not None:
                        try:
                            if callable(publish_log):
                                publish_log(wait_event)
                            else:
                                self.logger.record('run.waiting', {'kind': waiting.kind})
                        except Exception:
                            pass  # Diagnostic failure cannot undo the committed wait.
                except KeyboardInterrupt:
                    session = self.engine.current_session
                    summary = ExecutionSummary('Interrupted', list(session.tool_results), outcome='interrupted', checkpoint=session.checkpoint)
                    summary = self.engine._finish(summary)
                    self.last_summary = summary
                    self.task_store.finish(record.submission_id, summary, run_id=getattr(self.logger, 'last_run_id', None))
                    if self.logger is not None:
                        self.logger.finalize(bound_request, summary)
                    raise
                finally:
                    try:
                        self.close_tool_state()
                    finally:
                        detach_log = getattr(self.logger, 'detach_run', None)
                        if callable(detach_log):
                            detach_log()
                return self.get_submission(record.submission_id)
            except BaseException as error:
                if not claimed:
                    raise
                if saved and not context.execution_started and not isinstance(error, KeyboardInterrupt):
                    checkpoint_log = getattr(self.logger, 'checkpoint_run', None)
                    if callable(checkpoint_log) and getattr(self.logger, 'run_id', None):
                        saved['payload']['logger'] = checkpoint_log()
                    self.task_store.restore_wait(record.submission_id, saved['payload'], error=type(error).__name__)
                    try:
                        self.close_tool_state()
                    except Exception:
                        pass  # The original preparation failure remains retryable.
                    finally:
                        detach_log = getattr(self.logger, 'detach_run', None)
                        if callable(detach_log):
                            detach_log()
                    raise
                partial = self.last_summary or getattr(self.engine, "last_failure_summary", None)
                if isinstance(error, KeyboardInterrupt) and partial is None and saved:
                    from .orchestration.continuation import restore
                    restored, *_ = restore(saved['payload'], context.cancellation)
                    partial = ExecutionSummary('Interrupted', restored.tool_results, outcome='interrupted',
                                               checkpoint=restored.checkpoint, workspace_state=restored.workspace_state)
                    self.last_summary = partial
                    if self.logger is not None and self.logger.run_dir is not None:
                        self.logger.finalize(bound_request, partial)
                if background and bound_request.metadata.get("defer_finalize"):
                    self._pending_submission = record.submission_id
                    raise
                self.task_store.finish(record.submission_id, partial,
                                       run_id=getattr(self.logger, "last_run_id", None),
                                       error=type(error).__name__)
                if isinstance(error, KeyboardInterrupt) and not _defer_terminal_notification:
                    self._notify_terminal(partial, background=background)
                raise
            else:
                if background and bound_request.metadata.get("defer_finalize"):
                    self._pending_submission = record.submission_id
                    return summary
                self.task_store.finish(record.submission_id, summary,
                                       run_id=getattr(self.logger, "last_run_id", None))
                if not _defer_terminal_notification:
                    self._notify_terminal(summary, background=background)
                return summary
            finally:
                self._active_context = None
                if self._pending_submission != record.submission_id:
                    self._release_task_context(record.task_id)
                if "last_session_user_message" in bound_request.metadata:
                    request.metadata["last_session_user_message"] = bound_request.metadata["last_session_user_message"]


    def _retain_task_context(self, task_id: str) -> None:
        self._task_contexts.setdefault(task_id, ExecutionContext(task_id=task_id))
        self._task_context_users[task_id] = self._task_context_users.get(task_id, 0) + 1

    def _release_task_context(self, task_id: str) -> None:
        with self._condition:
            remaining = self._task_context_users.get(task_id, 0) - 1
            if remaining > 0:
                self._task_context_users[task_id] = remaining
                return
            self._task_context_users.pop(task_id, None)
            self._task_contexts.pop(task_id, None)

    def _notify_terminal(self, summary: ExecutionSummary | None, *, background: bool) -> None:
        if background or summary is None:
            return
        name = "on_run_interrupted" if getattr(summary, "outcome", None) == "interrupted" else "on_run_finished"
        self._notify(name, summary)

    def _notify(self, name: str, value) -> None:
        callback = getattr(self, name)
        if callback is None:
            return
        try:
            callback(value)
        except Exception as error:
            if self.logger is not None:
                try:
                    self.logger.record("observer.error", {
                        "observer": name,
                        "error": type(error).__name__,
                    })
                except Exception:
                    pass

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
        with self._condition:
            waiting = next(iter(self._synchronous_waits)) if len(self._synchronous_waits) == 1 else None
        if waiting is not None and self._active_context is None:
            self.cancel_submission(waiting)
            return
        context = self._active_context
        if context is not None:
            context.cancellation.set()
            cancel_bound = getattr(self.engine, "cancel_execution", None)
            if callable(cancel_bound):
                cancel_bound(context)
            elif getattr(self.engine, "_execution_context", None) is context:
                self.engine.cancel_current_run()
        else:
            cancel = getattr(self.engine, "cancel_current_run", None)
            if callable(cancel):
                cancel()

    def cancel_submission(self, submission_id: str):
        record = self.get_submission(submission_id)
        if record is None:
            raise KeyError(submission_id)
        control = self.grant_task_control(record.task_id, {'cancel'}, actor='local-submission-owner')
        try:
            return self.cancel(control)
        finally:
            self.revoke_task_control(control)

    @property
    def is_running(self) -> bool:
        with self._condition:
            return self._running or bool(self._queue)

    @property
    def is_closed(self) -> bool:
        with self._condition:
            return self._closed

    def close(self, *, force: bool = False) -> None:
        """Stop accepting work; retry incomplete cleanup on subsequent calls."""
        with self._condition:
            if not self._closed:
                if (self._running or self._queue) and not force:
                    raise RuntimeError("Runtime is still running")
                self._closed = True
                self._task_access.clear()
                self._task_control.clear()
                self._condition.notify_all()
            running = self._running
        if running:
            self.cancel_current_run()
            return
        self._finish_close()

    def _finish_close(self) -> None:
        with self._shutdown_lock:
            if self._close_cleanup_complete:
                return
            self.close_tool_state()
            self._close_cleanup_complete = True

    def configure_interaction(self, *, approval_callback=None, progress_reporter=None, user_input_callback=None) -> None:
        """Attach shell interaction adapters without exposing engine mutation."""
        self.engine.approval_callback = approval_callback
        self.engine.progress_reporter = progress_reporter
        self.user_input_callback = user_input_callback

    def run_background(self, request: UserRequest) -> ExecutionSummary:
        return self.execute(request, background=True)

    def execute_persisted_run(
        self,
        request: UserRequest,
        session: StoredSession,
        *,
        status: str = "active",
        close_runtime: bool = False,
    ) -> ExecutionSummary:
        submission_id = request.metadata.get("runtime_submission_id")
        if not isinstance(submission_id, str) or not submission_id.strip():
            submission_id = uuid4().hex
            request.metadata["runtime_submission_id"] = submission_id
        try:
            summary = self.execute(
                request,
                submission_id=submission_id,
                _session=session,
                _defer_terminal_notification=True,
            )
        except KeyboardInterrupt:
            summary = self.last_summary
            if isinstance(summary, ExecutionSummary):
                delivered = self._deliver_submission(submission_id, lambda: self.persist_run(
                    session,
                    request.prompt,
                    summary,
                    model_user_content=request.metadata.get("last_session_user_message"),
                    status=status,
                    close_runtime=close_runtime,
                ))
                if delivered:
                    self._notify_terminal(summary, background=False)
            raise
        delivered = self._deliver_submission(submission_id, lambda: self.persist_run(
            session,
            request.prompt,
            summary,
            model_user_content=request.metadata.get("last_session_user_message"),
            status=status,
            close_runtime=close_runtime,
        ))
        if delivered:
            self._notify_terminal(summary, background=False)
        return summary

    def _execute(self, request: UserRequest, *, background: bool, context: ExecutionContext | None = None, session=None) -> ExecutionSummary:
        if self.subagent_grant is not None:
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
            try:
                self.logger.record(
                    "runtime.model_backend",
                    {"type": type(getattr(self.engine, "model", None)).__name__},
                )
            except Exception:
                # Observability must never turn a valid run into a failed run.
                pass
            if session is not None and self.session_store is not None:
                self._attach_last_run_id(session)
                self.session_store.save(session)
        if not background:
            self._notify("on_run_started", request)
        try:
            execute_in_context = getattr(self.engine, "execute_in_context", None)
            if context is not None:
                context.execution_started = True
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
            raise
        except Exception as error:
            if context is not None and not context.execution_started:
                raise
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
                    final_message=self._runtime_error_message(error),
                    tool_results=[], outcome="runtime_error",
                )
            if self.logger is not None:
                self.logger.record("run.error", {"message": str(error)})
        self.last_summary = summary
        if self.logger is not None and not (background and request.metadata.get("defer_finalize")):
            self.logger.finalize(request, summary)
        return summary

    @staticmethod
    def _runtime_error_message(error: RuntimeError) -> str:
        return runtime_failure_message(error)

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
            submission_id = self._pending_submission
            record = self.task_store.get_submission(submission_id)
            if record.result is None:
                self.task_store.finish(submission_id, summary,
                                       run_id=getattr(self.logger, "last_run_id", None))
            self._deliver_submission(submission_id, lambda: self.sessions.persist_run(
                session, prompt, summary, model_user_content=model_user_content,
                status=status, close_runtime=close_runtime))
            self._release_task_context(record.task_id)
            self._pending_submission = None
            return None
        return self.sessions.persist_run(session, prompt, summary, model_user_content=model_user_content,
                                         status=status, close_runtime=close_runtime)

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
