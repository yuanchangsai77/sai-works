from __future__ import annotations

from pathlib import Path

from ..types import ExecutionSummary, SessionRecord, StoredSession, UserRequest, WorkspaceSessionState
from .messages import SESSION_REQUEST_MESSAGE_FORMAT


class SessionCoordinator:
    """Session persistence, context policy and recovery coordination without a shell."""

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def engine(self):
        return self.runtime.engine

    @property
    def logger(self):
        return self.runtime.logger

    @property
    def session_store(self):
        return self.runtime.session_store

    @property
    def subagent_coordinator(self):
        return self.runtime.subagent_coordinator

    def close_tool_state(self) -> None:
        reset = getattr(getattr(self.engine, "tools", None), "reset_state", None)
        if callable(reset):
            reset()

    def close_session(self, session, conversation) -> None:
        try:
            if session is not None and self.session_store is not None:
                session.status = "closed"
                run_summary = getattr(self.logger, "last_run_summary", None)
                if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
                    session.trace.append(run_summary)
                self._attach_last_run_id(session)
                session.messages = list(conversation)
                self.session_store.save(session)
        finally:
            self.close_tool_state()

    def list_sessions(self) -> list[SessionRecord]:
        if self.session_store is None:
            return []
        return self.session_store.list_sessions()

    def load_session(self, session_id: str) -> StoredSession | None:
        if self.session_store is None:
            return None
        return self.session_store.load(session_id)

    def prepare_session_runtime(self, session: StoredSession | None) -> None:
        if session is None:
            return
        prepare = getattr(self.engine, "prepare_session_state", None)
        if callable(prepare):
            prepare(
                session.session_id,
                getattr(session, "active_capability_ids", []),
            )

    def latest_session(self) -> StoredSession | None:
        if self.session_store is None:
            return None
        return self.session_store.latest()

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
        if self.session_store is None:
            return
        # Normal foreground runs are finalized by _run_once. Background subagents
        # deliberately defer that step until the runner has validated the final
        # outcome, leaving the logger's run open here.
        if self.logger is not None and getattr(self.logger, "run_dir", None) is not None:
            request = UserRequest(
                prompt=prompt,
                cwd=session.cwd,
                metadata={"session_id": session.session_id},
            )
            self.logger.finalize(request, summary)
        user_message = {
            "role": "user",
            "content": model_user_content if isinstance(model_user_content, str) else prompt,
        }
        if isinstance(model_user_content, str):
            user_message["session_message_format"] = SESSION_REQUEST_MESSAGE_FORMAT
        session.messages.extend([user_message, {"role": "assistant", "content": summary.final_message}])
        session.status = status
        session.active_capability_ids = list(
            getattr(summary, "active_capability_ids", [])
        )
        self._apply_workspace_summary(session, summary)
        run_summary = getattr(self.logger, "last_run_summary", None)
        if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
            session.trace.append(run_summary)
        self._attach_last_run_id(session)
        try:
            self.session_store.save(session)
            if (
                session.session_role == "primary"
                and session.cluster_id
                and self.subagent_coordinator is not None
            ):
                self.subagent_coordinator.update_member_state(
                    session,
                    self._cluster_state_for_outcome(summary.outcome),
                )
        finally:
            if close_runtime:
                tools = getattr(self.engine, "tools", None)
                reset_state = getattr(tools, "reset_state", None)
                if callable(reset_state):
                    reset_state()

    @staticmethod
    def _apply_workspace_summary(session, summary) -> None:
        state = getattr(summary, "workspace_state", None)
        if isinstance(state, WorkspaceSessionState) and state.active_root:
            session.workspace_state = state
            session.cwd = state.active_root

    @staticmethod
    def _workspace_root(session: StoredSession | None, fallback: str) -> str:
        state = getattr(session, "workspace_state", None)
        if isinstance(state, WorkspaceSessionState) and state.active_root:
            return state.active_root
        return fallback

    @staticmethod
    def _ensure_workspace_state(session: StoredSession, fallback: str) -> None:
        state = getattr(session, "workspace_state", None)
        if not isinstance(state, WorkspaceSessionState):
            session.workspace_state = WorkspaceSessionState(
                origin_root=session.cwd or fallback,
                active_root=session.cwd or fallback,
            )
            return
        state.origin_root = state.origin_root or session.cwd or fallback
        state.active_root = state.active_root or session.cwd or fallback

    @staticmethod
    def _cluster_state_for_outcome(outcome: str) -> str:
        if outcome == "completed":
            return "completed"
        if outcome in {"blocked", "stalled", "exhausted"}:
            return "blocked"
        if outcome in {"interrupted", "cancelled"}:
            return "cancelled"
        return "failed"

    def _attach_last_run_id(self, session: StoredSession) -> None:
        run_id = getattr(self.logger, "last_run_id", None)
        if isinstance(run_id, str) and run_id and run_id not in session.run_ids:
            session.run_ids.append(run_id)

    def append_turn_and_persist(
        self,
        request: UserRequest,
        prompt: str,
        summary: ExecutionSummary,
        session: StoredSession | None,
        conversation: list[dict[str, str]],
        context_generation: int,
    ) -> None:
        model_user_message = request.metadata.get("last_session_user_message")
        user_message = {
            "role": "user",
            "content": model_user_message if isinstance(model_user_message, str) else prompt,
        }
        if isinstance(model_user_message, str):
            user_message["session_message_format"] = SESSION_REQUEST_MESSAGE_FORMAT
        conversation.extend([user_message, {"role": "assistant", "content": summary.final_message}])
        if session is None or self.session_store is None:
            return

        session.messages = list(conversation)
        self._apply_workspace_summary(session, summary)
        session.status = "active"
        session.active_capability_ids = list(getattr(summary, "active_capability_ids", []))
        run_summary = getattr(self.logger, "last_run_summary", None)
        if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
            session.trace.append(run_summary)
        self._attach_last_run_id(session)
        session.context_generation = context_generation
        self.session_store.save(session)
        if session.context_generation > context_generation:
            conversation.clear()
            conversation.extend(session.messages)

    def execute_session_turn(
        self,
        request: UserRequest,
        prompt: str,
        session: StoredSession | None,
        conversation: list[dict[str, str]],
        context_generation: int,
    ) -> ExecutionSummary | None:
        store = self.session_store
        if session is not None and store is not None:
            with store.conversation_lock(session.session_id):
                latest = store.load(session.session_id)
                if latest is not None and (
                    latest.context_generation != context_generation
                    or latest.revision != session.revision
                ):
                    session.cwd = latest.cwd
                    session.messages = list(latest.messages)
                    session.run_ids = list(latest.run_ids)
                    session.active_capability_ids = list(latest.active_capability_ids)
                    session.trace = list(latest.trace)
                    session.resume_state = latest.resume_state
                    session.workspace_state = latest.workspace_state
                    session.revision = latest.revision
                    session.compaction_archive_ids = list(latest.compaction_archive_ids)
                    session.context_generation = latest.context_generation
                    session.context_trace_after_run_id = latest.context_trace_after_run_id
                    conversation.clear()
                    conversation.extend(session.messages)
                    return None

                if session.cluster_id and self.subagent_coordinator is not None:
                    self.subagent_coordinator.update_member_state(session, "running")
                summary = self.runtime.execute(request, _session=session)
                self.append_turn_and_persist(
                    request, prompt, summary, session, conversation, context_generation
                )
                if session.cluster_id and self.subagent_coordinator is not None:
                    self.subagent_coordinator.update_member_state(
                        session, self._cluster_state_for_outcome(summary.outcome)
                    )
                return summary

        return self.runtime.execute(request)

    def compaction_needed(self, conversation, pending_content: str) -> bool:
        store = self.session_store
        if store is None:
            return False
        turns = [item for item in conversation if item.get("role") in {"user", "assistant"}
                 and isinstance(item.get("content"), str)]
        if len(turns) < 2:
            return False
        profile = getattr(getattr(self.engine, "model", None), "capability_profile", None)
        try:
            model_budget = int(getattr(profile, "context_budget_chars", store.max_message_chars))
        except (TypeError, ValueError):
            model_budget = store.max_message_chars
        budget = min(store.max_message_chars, max(1, model_budget))
        chars = sum(len(item.get("content", "")) for item in conversation
                    if isinstance(item.get("content"), str)) + len(pending_content)
        return chars >= int(budget * 0.75)

    def compact_session(self, session, conversation):
        from .compaction import compact, copy_session_context

        def apply():
            with self.runtime.execution_slot():
                return compact(self.session_store, session, conversation, getattr(self.engine, "model", None))

        if session is None or self.session_store is None:
            return apply()
        with self.session_store.conversation_lock(session.session_id):
            latest = self.session_store.load(session.session_id)
            if latest is not None:
                copy_session_context(latest, session)
                conversation[:] = latest.messages
            return apply()

    def open_session(self, cwd, conversation, session_id=None):
        if self.session_store is None:
            return None
        session = self.load_session(session_id) if session_id else None
        if session is None:
            session = self.session_store.create(cwd=cwd, messages=conversation)
        else:
            self._ensure_workspace_state(session, cwd)
            session.status = "active"
            self.session_store.save(session)
            conversation[:] = session.messages
        self.prepare_session_runtime(session)
        return session

    def prepare_turn(self, session, request, generation, conversation) -> bool:
        if session is None or self.session_store is None:
            return True
        self.session_store.save(session)
        if session.context_generation > generation:
            conversation[:] = session.messages
            return False
        return True

    def discard_prepared_turn(self, request) -> None:
        if self.logger is not None:
            self.logger.finalize(request, ExecutionSummary(
                final_message="Request refreshed after context replacement.", tool_results=[], outcome="interrupted"))

    def save_interrupted_session(self, session) -> None:
        if session is None or self.session_store is None:
            return
        run_summary = getattr(self.logger, "last_run_summary", None)
        if run_summary is not None and all(item.run_id != run_summary.run_id for item in session.trace):
            session.trace.append(run_summary)
        self._attach_last_run_id(session)
        self.session_store.save(session)
        if session.cluster_id and self.subagent_coordinator is not None:
            self.subagent_coordinator.update_member_state(session, "cancelled")

    def save_session(self, session) -> None:
        if self.session_store is not None:
            self.session_store.save(session)

    def reset_session(self, session, conversation):
        conversation.clear()
        if session is None or self.session_store is None:
            if session is not None:
                session.messages.clear()
            return session
        origin = getattr(getattr(session, "workspace_state", None), "origin_root", "") or session.cwd
        new = self.session_store.create(cwd=origin, messages=[])
        new.active_capability_ids = list(session.active_capability_ids)
        new.trace = list(session.trace)
        new.run_ids = list(session.run_ids)
        self.session_store.save(new)
        session.cwd, session.session_id, session.messages = new.cwd, new.session_id, list(new.messages)
        self.prepare_session_runtime(new)
        return new

    def load_archived_messages(self, session_id, archive_id):
        return self.session_store.load_archived_messages(session_id, archive_id) if self.session_store else None

    def build_session_request(self, prompt, cwd, session, conversation, context_paths):
        active_capability_ids = []
        session_trace = []
        resume_state = None
        if session is not None:
            active_capability_ids = getattr(session, "active_capability_ids", [])
            session_trace = list(getattr(session, "trace", []))
            trace_anchor = getattr(session, "context_trace_after_run_id", "")
            if trace_anchor:
                anchor_index = next(
                    (
                        index
                        for index, item in enumerate(session_trace)
                        if getattr(item, "run_id", "") == trace_anchor
                    ),
                    None,
                )
                if anchor_index is not None:
                    session_trace = session_trace[anchor_index + 1 :]
                else:
                    # If the anchor was trimmed from the bounded trace list,
                    # none of the remaining entries can be proven to belong
                    # to the active compacted generation.
                    session_trace = []
            elif getattr(session, "context_generation", 0) > 0:
                session_trace = []
            session_trace = session_trace[-6:]
            resume_state = getattr(session, "resume_state", None)
            if getattr(session, "context_generation", 0) > 0:
                if not trace_anchor:
                    resume_state = None
                else:
                    trace_ids = [getattr(item, "run_id", "") for item in session.trace]
                    anchor_index = next(
                        (index for index, run_id in enumerate(trace_ids) if run_id == trace_anchor),
                        None,
                    )
                    resume_run_id = getattr(resume_state, "last_run_id", "")
                    if anchor_index is None or resume_run_id in trace_ids[: anchor_index + 1]:
                        resume_state = None

        return UserRequest(
            prompt=prompt,
            cwd=self._workspace_root(session, cwd),
            metadata={
                "conversation": list(conversation),
                "session_id": session.session_id if session is not None else None,
                "active_capability_ids": list(active_capability_ids),
                "session_trace": session_trace,
                "resume_state": resume_state,
                "context_paths": list(context_paths or []),
                "workspace_state": getattr(session, "workspace_state", None),
            },
        )

    def session_usage(self, session):
        import json

        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        has_usage = False

        if session is not None and self.logger is not None and getattr(self.logger, "base_dir", None) is not None:
            run_ids = list(session.run_ids)
            current_run_id = getattr(self.logger, "run_id", None)
            if current_run_id and current_run_id not in run_ids:
                run_ids.append(current_run_id)

            for run_id in run_ids:
                run_dir = Path(self.logger.base_dir) / run_id
                events_file = run_dir / "events.jsonl"
                if events_file.exists():
                    try:
                        with events_file.open("r", encoding="utf-8") as f:
                            for line in f:
                                if not line.strip():
                                    continue
                                event = json.loads(line)
                                if event.get("name") == "model.response":
                                    payload = event.get("payload", {})
                                    usage = payload.get("usage")
                                    if isinstance(usage, dict):
                                        try:
                                            prompt_tokens += int(usage.get("prompt_tokens", 0))
                                            has_usage = True
                                        except (ValueError, TypeError):
                                            pass
                                        try:
                                            completion_tokens += int(usage.get("completion_tokens", 0))
                                            has_usage = True
                                        except (ValueError, TypeError):
                                            pass
                                        try:
                                            total_tokens += int(usage.get("total_tokens", 0))
                                            has_usage = True
                                        except (ValueError, TypeError):
                                            pass
                    except Exception:
                        pass

        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": total_tokens, "has_usage": has_usage}
