from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
from typing import Iterator

from ..types import (
    EvidenceRecord,
    RuntimeBlocker,
    SessionRecord,
    SessionResumeState,
    SessionRunTrace,
    SessionTurnTrace,
    StoredSession,
    TaskCheckpoint,
    WorkspaceSessionState,
)
from .messages import (
    SESSION_REQUEST_MESSAGE_FORMAT,
    display_session_message,
    model_session_message_content,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


DEFAULT_SESSION_MESSAGE_CHARS = 120_000
MAX_SESSION_MESSAGE_CHARS = 2_000_000
MAX_SESSION_TRACES = 24
MAX_TRACE_TURNS = 20


class SessionStore:
    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        max_message_chars: int | None = None,
    ) -> None:
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
        self.base_dir = root / ".saiworks" / "sessions"
        requested_limit = (
            DEFAULT_SESSION_MESSAGE_CHARS
            if max_message_chars is None
            else int(max_message_chars)
        )
        self.max_message_chars = min(
            MAX_SESSION_MESSAGE_CHARS,
            max(DEFAULT_SESSION_MESSAGE_CHARS, requested_limit),
        )

    def create(
        self,
        cwd: str,
        messages: list[dict[str, str]] | None = None,
        *,
        parent_session_id: str = "",
        cluster_id: str = "",
        session_role: str = "primary",
        launch_source: str = "direct",
        session_image_id: str = "",
    ) -> StoredSession:
        now = self._timestamp()
        session = StoredSession(
            session_id=self._build_session_id(now),
            cwd=cwd,
            created_at=now,
            updated_at=now,
            status="active",
            messages=list(messages or []),
            run_ids=[],
            trace=[],
            resume_state=SessionResumeState(),
            workspace_state=WorkspaceSessionState(origin_root=cwd, active_root=cwd),
            parent_session_id=parent_session_id,
            cluster_id=cluster_id,
            session_role=session_role,
            launch_source=launch_source,
            session_image_id=session_image_id,
        )
        self.save(session)
        return session

    def save(self, session: StoredSession) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        path = self.base_dir / f"{session.session_id}.json"
        with _locked(path.with_suffix(".lock")):
            existing: dict[str, object] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        existing = loaded
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    existing = {}
            self._merge_stale_session(session, existing)
            updated_at = self._timestamp()
            session.updated_at = updated_at
            session.revision = int(existing.get("revision", 0)) + 1
            session.run_ids = list(dict.fromkeys(session.run_ids))
            session.trace = list(session.trace[-MAX_SESSION_TRACES:])
            latest_trace = session.trace[-1] if session.trace else None
            if (
                latest_trace is not None
                and latest_trace.run_id != session.resume_state.last_run_id
                and latest_trace.run_id != session.context_trace_after_run_id
            ):
                session.resume_state = self._build_resume_state(session)
            payload = {
            "session_id": session.session_id,
            "cwd": session.cwd,
            "created_at": session.created_at,
            "updated_at": updated_at,
            "status": session.status,
            "messages": session.messages,
            "run_ids": session.run_ids,
            "active_capability_ids": list(getattr(session, "active_capability_ids", [])),
            "trace": [self._trace_to_payload(item) for item in getattr(session, "trace", [])],
            "resume_state": self._resume_state_to_payload(session.resume_state),
            "workspace_state": self._workspace_state_to_payload(session.workspace_state, session.cwd),
            "parent_session_id": session.parent_session_id,
            "cluster_id": session.cluster_id,
            "session_role": session.session_role,
            "launch_source": session.launch_source,
            "session_image_id": session.session_image_id,
            "compaction_archive_ids": session.compaction_archive_ids,
            "context_generation": session.context_generation,
            "context_trace_after_run_id": session.context_trace_after_run_id,
            "revision": session.revision,
            }
            _atomic_text_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            # The JSON snapshot is authoritative. A failure to refresh its
            # derived text log must not make callers roll back a committed save.
            try:
                self._write_trace_log(session)
            except OSError:
                pass
            replay_path = self.base_dir / f"{session.session_id}.replay.log"
            if replay_path.exists():
                try:
                    replay_path.unlink()
                except OSError:
                    pass

    @contextmanager
    def conversation_lock(self, session_id: str) -> Iterator[None]:
        """Serialize model turns and context replacement for one session."""
        if not self._valid_session_id(session_id):
            raise ValueError("invalid session id")
        lock_path = self.base_dir / f"{session_id}.conversation.lock"
        with _locked(lock_path):
            yield

    def load(self, session_id: str) -> StoredSession | None:
        if not self._valid_session_id(session_id):
            return None

        path = self.base_dir / f"{session_id}.json"
        if not path.exists():
            return None

        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        messages = self._normalize_messages(payload.get("messages", []))
        return StoredSession(
            session_id=str(payload["session_id"]),
            cwd=str(payload["cwd"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            status=str(payload.get("status", "active")),
            messages=messages,
            run_ids=self._normalize_run_ids(payload.get("run_ids", [])),
            active_capability_ids=self._string_list(payload.get("active_capability_ids", [])),
            trace=self._normalize_trace(payload.get("trace", [])),
            resume_state=self._normalize_resume_state(payload.get("resume_state", {})),
            workspace_state=self._normalize_workspace_state(payload.get("workspace_state", {}), str(payload["cwd"])),
            parent_session_id=str(payload.get("parent_session_id", "")),
            cluster_id=str(payload.get("cluster_id", "")),
            session_role=str(payload.get("session_role", "primary")),
            launch_source=str(payload.get("launch_source", "direct")),
            session_image_id=str(payload.get("session_image_id", "")),
            revision=self._safe_int(payload.get("revision"), 0),
            compaction_archive_ids=self._string_list(payload.get("compaction_archive_ids", [])),
            context_generation=self._safe_int(payload.get("context_generation"), 0),
            context_trace_after_run_id=str(payload.get("context_trace_after_run_id", "")),
        )

    def archive_messages(
        self,
        session_id: str,
        messages: list[dict[str, str]],
    ) -> str:
        if not self._valid_session_id(session_id):
            raise ValueError("invalid session id")
        archive_id = uuid4().hex
        archive_dir = self.base_dir / "archives" / session_id
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_messages = [
            dict(item)
            for item in messages
            if isinstance(item, dict)
            and isinstance(item.get("role"), str)
            and isinstance(item.get("content"), str)
        ]
        payload = {
            "session_id": session_id,
            "archive_id": archive_id,
            "created_at": self._timestamp(),
            "messages": archived_messages,
        }
        path = archive_dir / f"{archive_id}.json"
        _atomic_text_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return archive_id

    def load_archived_messages(
        self,
        session_id: str,
        archive_id: str,
    ) -> list[dict[str, str]] | None:
        if not self._valid_session_id(session_id):
            return None
        try:
            if UUID(archive_id).hex != archive_id:
                return None
        except (ValueError, AttributeError, TypeError):
            return None
        path = self.base_dir / "archives" / session_id / f"{archive_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("session_id") != session_id
            or payload.get("archive_id") != archive_id
            or not isinstance(payload.get("messages"), list)
        ):
            return None
        return [
            dict(item)
            for item in payload["messages"]
            if isinstance(item, dict)
            and isinstance(item.get("role"), str)
            and isinstance(item.get("content"), str)
        ]

    def _merge_stale_session(self, session: StoredSession, existing: dict[str, object]) -> None:
        if not existing:
            return
        existing_revision = self._safe_int(existing.get("revision"), 0)
        existing_generation = self._safe_int(existing.get("context_generation"), 0)
        if not session.cluster_id:
            session.cluster_id = str(existing.get("cluster_id", ""))
            session.parent_session_id = str(existing.get("parent_session_id", session.parent_session_id))
            session.session_role = str(existing.get("session_role", session.session_role))
            session.launch_source = str(existing.get("launch_source", session.launch_source))
            session.session_image_id = str(existing.get("session_image_id", session.session_image_id))
        stale_context_generation = session.context_generation < existing_generation
        if session.revision >= existing_revision and not stale_context_generation:
            return
        session.cwd = str(existing.get("cwd", session.cwd))
        session.created_at = str(existing.get("created_at", session.created_at))
        session.cluster_id = str(existing.get("cluster_id", session.cluster_id))
        session.parent_session_id = str(existing.get("parent_session_id", session.parent_session_id))
        session.session_role = str(existing.get("session_role", session.session_role))
        session.launch_source = str(existing.get("launch_source", session.launch_source))
        session.session_image_id = str(existing.get("session_image_id", session.session_image_id))
        persisted_status = str(existing.get("status", "active"))
        terminal_statuses = {"blocked", "cancelled", "closed", "completed", "failed"}
        if persisted_status in terminal_statuses:
            session.status = persisted_status
        persisted_messages = self._normalize_messages(existing.get("messages", []))
        if stale_context_generation:
            # A stale writer must not restore messages removed by compaction.
            session.messages = persisted_messages
            session.context_generation = existing_generation
            session.context_trace_after_run_id = str(
                existing.get("context_trace_after_run_id", "")
            )
        elif session.context_generation == existing_generation:
            for message in persisted_messages:
                if message not in session.messages:
                    session.messages.append(message)
            session.context_trace_after_run_id = str(
                existing.get("context_trace_after_run_id", session.context_trace_after_run_id)
            )
        persisted_archives = self._string_list(existing.get("compaction_archive_ids", []))
        session.compaction_archive_ids = list(
            dict.fromkeys([*persisted_archives, *session.compaction_archive_ids])
        )
        persisted_runs = self._normalize_run_ids(existing.get("run_ids", []))
        session.run_ids = list(dict.fromkeys([*persisted_runs, *session.run_ids]))
        persisted_capabilities = self._string_list(existing.get("active_capability_ids", []))
        session.active_capability_ids = list(
            dict.fromkeys([*persisted_capabilities, *session.active_capability_ids])
        )
        persisted_trace = self._normalize_trace(existing.get("trace", []))
        if stale_context_generation:
            # Runs that started in the old context generation can contain the
            # archived transcript in their prompts, tool results, or checkpoint.
            session.trace = persisted_trace
        else:
            persisted_run_ids = {item.run_id for item in persisted_trace}
            session.trace = [
                *persisted_trace,
                *[item for item in session.trace if item.run_id not in persisted_run_ids],
            ]
        if session.context_generation == existing_generation:
            session.resume_state = self._normalize_resume_state(existing.get("resume_state", {}))
        session.workspace_state = self._normalize_workspace_state(
            existing.get("workspace_state", {}), session.cwd
        )

    def list_sessions(self) -> list[SessionRecord]:
        if not self.base_dir.exists():
            return []

        sessions: list[SessionRecord] = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                session = self.load(path.stem)
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue

            if session is None:
                continue

            preview = ""
            for message in session.messages:
                if message.get("role") == "user":
                    preview = self._preview(display_session_message(message))
                    break

            sessions.append(
                SessionRecord(
                    session_id=session.session_id,
                    cwd=session.cwd,
                    created_at=session.created_at,
                    updated_at=session.updated_at,
                    status=session.status,
                    message_count=len(session.messages),
                    preview=preview,
                )
            )

        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def latest(self) -> StoredSession | None:
        sessions = self.list_sessions()
        if not sessions:
            return None
        return self.load(sessions[0].session_id)

    def _normalize_messages(self, messages: object) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        if not isinstance(messages, list):
            return normalized

        for item in messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if isinstance(role, str) and isinstance(content, str):
                message = {"role": role, "content": content}
                display_content = item.get("display_content")
                message_format = item.get("session_message_format")
                if message_format == SESSION_REQUEST_MESSAGE_FORMAT:
                    message["session_message_format"] = message_format
                elif role == "user" and content.startswith("Current working directory:"):
                    message["content"] = model_session_message_content(item)
                elif isinstance(display_content, str) and display_content != content:
                    message["display_content"] = display_content
                normalized.append(message)
        return normalized

    def _normalize_run_ids(self, run_ids: object) -> list[str]:
        if not isinstance(run_ids, list):
            return []
        return [item for item in run_ids if isinstance(item, str) and item]

    def _normalize_trace(self, trace: object) -> list[SessionRunTrace]:
        if not isinstance(trace, list):
            return []

        normalized: list[SessionRunTrace] = []
        for item in trace[-MAX_SESSION_TRACES:]:
            if not isinstance(item, dict):
                continue
            turns_payload = item.get("turns", [])
            turns: list[SessionTurnTrace] = []
            if isinstance(turns_payload, list):
                for turn in turns_payload[-MAX_TRACE_TURNS:]:
                    if not isinstance(turn, dict):
                        continue
                    turns.append(
                        SessionTurnTrace(
                            turn=self._safe_int(turn.get("turn"), 0),
                            message=str(turn.get("message", "")),
                            actions=self._string_list(turn.get("actions")),
                            tool_results=self._string_list(turn.get("tool_results")),
                            action_details=self._string_list(turn.get("action_details")),
                            tool_result_details=self._string_list(turn.get("tool_result_details")),
                        )
                    )
            normalized.append(
                SessionRunTrace(
                    run_id=str(item.get("run_id", "")),
                    started_at=str(item.get("started_at", "")),
                    completed_at=str(item.get("completed_at", "")),
                    prompt=str(item.get("prompt", "")),
                    final_message=str(item.get("final_message", "")),
                    outcome=str(item.get("outcome", "completed")),
                    event_count=self._safe_int(item.get("event_count"), 0),
                    turn_count=self._safe_int(item.get("turn_count"), len(turns)),
                    tool_names=self._string_list(item.get("tool_names")),
                    turns=turns,
                    blockers=self._normalize_blockers(item.get("blockers", [])),
                    checkpoint=self._normalize_checkpoint(item.get("checkpoint", {})),
                )
            )
        return normalized

    def _normalize_resume_state(self, payload: object) -> SessionResumeState:
        if not isinstance(payload, dict):
            return SessionResumeState()
        return SessionResumeState(
            last_run_id=str(payload.get("last_run_id", "")),
            last_user_prompt=str(payload.get("last_user_prompt", "")),
            last_assistant_message=str(payload.get("last_assistant_message", "")),
            last_outcome=str(payload.get("last_outcome", "")),
            last_tool_names=self._string_list(payload.get("last_tool_names")),
            open_issue=str(payload.get("open_issue", "")),
            recovery_hint=str(payload.get("recovery_hint", "")),
            blockers=self._normalize_blockers(payload.get("blockers", [])),
            checkpoint=self._normalize_checkpoint(payload.get("checkpoint", {})),
        )

    def _normalize_workspace_state(self, payload: object, cwd: str) -> WorkspaceSessionState:
        if not isinstance(payload, dict):
            return WorkspaceSessionState(origin_root=cwd, active_root=cwd)
        origin_root = str(payload.get("origin_root", "")) or cwd
        active_root = str(payload.get("active_root", "")) or cwd
        return WorkspaceSessionState(
            origin_root=origin_root,
            active_root=active_root,
            approved_roots=self._string_list(payload.get("approved_roots", [])),
        )

    def _normalize_blockers(self, value: object) -> list[RuntimeBlocker]:
        if not isinstance(value, list):
            return []
        blockers: list[RuntimeBlocker] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            error_code = item.get("error_code")
            summary = item.get("summary")
            if not isinstance(error_code, str) or not isinstance(summary, str):
                continue
            blockers.append(
                RuntimeBlocker(
                    error_code=error_code,
                    summary=summary,
                    source=str(item.get("source", "runtime")),
                    tool=str(item.get("tool", "")),
                    retryability=str(item.get("retryability", "conditional")),
                    required_action=str(item.get("required_action", "resume")),
                )
            )
        return blockers

    def _normalize_checkpoint(self, value: object) -> TaskCheckpoint:
        if not isinstance(value, dict):
            return TaskCheckpoint()
        runtime_state = value.get("runtime_state", {})
        return TaskCheckpoint(
            objective=str(value.get("objective", "")),
            schema_version=max(3, self._safe_int(value.get("schema_version"), 1)),
            task_id=str(value.get("task_id", "")),
            workspace_root=str(value.get("workspace_root", "")),
            workspace_revision=max(0, self._safe_int(value.get("workspace_revision"), 0)),
            phase=str(value.get("phase", "in_progress")),
            completed_actions=self._string_list(value.get("completed_actions", [])),
            artifacts=self._string_list(value.get("artifacts", [])),
            evidence=self._normalize_evidence(value.get("evidence", [])),
            required_evidence=self._string_list(value.get("required_evidence", [])),
            unmet_deliverables=self._string_list(value.get("unmet_deliverables", [])),
            blockers=self._normalize_blockers(value.get("blockers", [])),
            runtime_state={
                str(key): str(item)
                for key, item in runtime_state.items()
                if isinstance(key, str) and isinstance(item, str)
            } if isinstance(runtime_state, dict) else {},
        )

    def _normalize_evidence(self, value: object) -> list[EvidenceRecord]:
        if not isinstance(value, list):
            return []
        records: list[EvidenceRecord] = []
        for item in value[-100:]:
            if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
                continue
            records.append(
                EvidenceRecord(
                    kind=item["kind"],
                    producer=str(item.get("producer", "unknown")),
                    task_id=str(item.get("task_id", "")),
                    workspace_revision=max(0, self._safe_int(item.get("workspace_revision"), 0)),
                    artifact_refs=self._string_list(item.get("artifact_refs", [])),
                    source_task_ids=self._string_list(item.get("source_task_ids", [])),
                )
            )
        return records

    def _string_list(self, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str) and item]

    def _safe_int(self, value: object, default: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _trace_to_payload(self, trace: SessionRunTrace) -> dict[str, object]:
        return {
            "run_id": trace.run_id,
            "started_at": trace.started_at,
            "completed_at": trace.completed_at,
            "prompt": trace.prompt,
            "final_message": trace.final_message,
            "outcome": trace.outcome,
            "event_count": trace.event_count,
            "turn_count": trace.turn_count,
            "tool_names": list(trace.tool_names),
            "turns": [
                {
                    "turn": turn.turn,
                    "message": turn.message,
                    "actions": list(turn.actions),
                    "tool_results": list(turn.tool_results),
                    "action_details": list(turn.action_details),
                    "tool_result_details": list(turn.tool_result_details),
                }
                for turn in trace.turns[-MAX_TRACE_TURNS:]
            ],
        }

    def _resume_state_to_payload(self, state: SessionResumeState) -> dict[str, object]:
        return {
            "last_run_id": state.last_run_id,
            "last_user_prompt": state.last_user_prompt,
            "last_assistant_message": state.last_assistant_message,
            "last_outcome": state.last_outcome,
            "last_tool_names": list(state.last_tool_names),
            "open_issue": state.open_issue,
            "recovery_hint": state.recovery_hint,
            "blockers": [self._blocker_to_payload(item) for item in state.blockers],
            "checkpoint": self._checkpoint_to_payload(state.checkpoint),
        }

    @staticmethod
    def _workspace_state_to_payload(
        state: WorkspaceSessionState,
        cwd: str,
    ) -> dict[str, object]:
        return {
            "origin_root": state.origin_root or cwd,
            "active_root": state.active_root or cwd,
            "approved_roots": list(dict.fromkeys(state.approved_roots)),
        }

    def _blocker_to_payload(self, blocker: RuntimeBlocker) -> dict[str, str]:
        return {
            "error_code": blocker.error_code,
            "summary": blocker.summary,
            "source": blocker.source,
            "tool": blocker.tool,
            "retryability": blocker.retryability,
            "required_action": blocker.required_action,
        }

    def _checkpoint_to_payload(self, checkpoint: TaskCheckpoint) -> dict[str, object]:
        return {
            "objective": checkpoint.objective,
            "schema_version": checkpoint.schema_version,
            "task_id": checkpoint.task_id,
            "workspace_root": checkpoint.workspace_root,
            "workspace_revision": checkpoint.workspace_revision,
            "phase": checkpoint.phase,
            "completed_actions": list(checkpoint.completed_actions),
            "artifacts": list(checkpoint.artifacts),
            "evidence": [
                {
                    "kind": item.kind,
                    "producer": item.producer,
                    "task_id": item.task_id,
                    "workspace_revision": item.workspace_revision,
                    "artifact_refs": list(item.artifact_refs),
                    "source_task_ids": list(item.source_task_ids),
                }
                for item in checkpoint.evidence
            ],
            "required_evidence": list(checkpoint.required_evidence),
            "unmet_deliverables": list(checkpoint.unmet_deliverables),
            "blockers": [self._blocker_to_payload(item) for item in checkpoint.blockers],
            "runtime_state": dict(checkpoint.runtime_state),
        }

    def _write_trace_log(self, session: StoredSession) -> None:
        trace_path = self.base_dir / f"{session.session_id}.trace.log"
        lines = [
            "Session Trace Summary",
            f"- session_id: {session.session_id}",
            f"- cwd: {session.cwd}",
            f"- status: {session.status}",
            f"- messages: {len(session.messages)}",
            f"- runs: {len(session.trace)}",
            "",
        ]
        state = session.resume_state
        if state.last_run_id or state.open_issue or state.recovery_hint:
            lines.extend(
                [
                    "Resume State",
                    f"- last_run_id: {state.last_run_id or '-'}",
                    f"- last_outcome: {state.last_outcome or '-'}",
                    (
                        f"- last_tools: {', '.join(state.last_tool_names)}"
                        if state.last_tool_names
                        else "- last_tools: -"
                    ),
                    f"- open_issue: {state.open_issue or '-'}",
                    f"- recovery_hint: {state.recovery_hint or '-'}",
                    "",
                ]
            )
        for trace in session.trace:
            lines.append(f"Run {trace.run_id}")
            lines.append(f"- prompt: {trace.prompt}")
            lines.append(f"- outcome: {trace.outcome}")
            lines.append(f"- turns: {trace.turn_count}")
            lines.append(f"- events: {trace.event_count}")
            if trace.tool_names:
                lines.append(f"- tools: {', '.join(trace.tool_names)}")
            lines.append(f"- final: {trace.final_message}")
            lines.append("")
        _atomic_text_write(trace_path, "\n".join(lines) + "\n")

    def _build_resume_state(self, session: StoredSession) -> SessionResumeState:
        trace = session.trace[-1] if session.trace else None
        last_user_prompt = ""
        last_assistant_message = ""
        for item in reversed(session.messages):
            if not last_assistant_message and item.get("role") == "assistant":
                last_assistant_message = item.get("content", "")
            elif not last_user_prompt and item.get("role") == "user":
                last_user_prompt = display_session_message(item)
            if last_user_prompt and last_assistant_message:
                break

        if trace is None:
            return SessionResumeState(
                last_user_prompt=last_user_prompt,
                last_assistant_message=last_assistant_message,
            )

        open_issue = ""
        recovery_hint = ""
        if trace.outcome != "completed":
            if trace.blockers:
                open_issue = trace.blockers[-1].summary
            else:
                open_issue = trace.final_message

        if open_issue:
            recovery_hint = (
                "Reuse the last successful result when possible, avoid repeating identical reads, "
                "and resolve the blocking issue before exploring more context."
            )
        elif trace.tool_names:
            recovery_hint = "Continue from the latest completed state and only call tools needed for the next step."

        return SessionResumeState(
            last_run_id=trace.run_id,
            last_user_prompt=trace.prompt or last_user_prompt,
            last_assistant_message=trace.final_message or last_assistant_message,
            last_outcome=trace.outcome,
            last_tool_names=list(trace.tool_names),
            open_issue=open_issue,
            recovery_hint=recovery_hint,
            blockers=self._normalize_blockers(
                [self._blocker_to_payload(item) for item in trace.blockers]
            ),
            checkpoint=self._normalize_checkpoint(
                self._checkpoint_to_payload(trace.checkpoint)
            ),
        )

    def _valid_session_id(self, session_id: str) -> bool:
        if not session_id or session_id in {".", ".."}:
            return False
        return Path(session_id).name == session_id

    def _preview(self, text: str, limit: int = 60) -> str:
        single_line = " ".join(text.split())
        if len(single_line) <= limit:
            return single_line
        return f"{single_line[: limit - 3]}..."

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _build_session_id(self, timestamp: str) -> str:
        compact = (
            timestamp.replace("-", "")
            .replace(":", "")
            .replace("T", "")
            .replace("Z", "")
            .replace(".", "")
        )
        return f"{compact}-{uuid4().hex[:8]}"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    acquired = False
    try:
        if fcntl is None and msvcrt is None:
            raise RuntimeError("file locking is unavailable on this platform")
        if fcntl is None:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
        while not acquired:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                else:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except (BlockingIOError, PermissionError):
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _atomic_text_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
