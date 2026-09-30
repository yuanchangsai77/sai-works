from __future__ import annotations

try:
    import readline
except ImportError:
    pass

from pathlib import Path

from ..sessions.messages import SESSION_REQUEST_MESSAGE_FORMAT
from ..types import ExecutionSummary, SessionRecord, StoredSession, UserRequest, WorkspaceSessionState
from ..runtime import Runtime
from .presenter import ConsolePresenter


from .commands import SlashCommandRegistry, default_slash_command_registry


class CLI:
    """Thin interaction shell that delegates work to the execution engine."""

    def __init__(
        self,
        engine,
        presenter: ConsolePresenter,
        logger=None,
        session_store=None,
        subagent_coordinator=None,
        subagent_runner=None,
        subagent_grant=None,
        command_registry: SlashCommandRegistry | None = None,
        runtime: Runtime | None = None,
    ) -> None:
        self.engine = engine
        self.presenter = presenter
        self.logger = logger
        self.session_store = session_store
        self.subagent_coordinator = subagent_coordinator
        self.subagent_runner = subagent_runner
        self.subagent_grant = subagent_grant
        self.command_registry = command_registry or default_slash_command_registry()
        if hasattr(self.presenter, "command_registry"):
            self.presenter.command_registry = self.command_registry
        if hasattr(self.presenter, "prompt_box") and hasattr(self.presenter.prompt_box, "_composer"):
            self.presenter.prompt_box._composer.command_registry = self.command_registry
        if hasattr(self.presenter, "_composer"):
            self.presenter._composer.command_registry = self.command_registry
            self.presenter._composer.command_context = self
        self.runtime = runtime or Runtime(
            engine, logger, session_store, subagent_coordinator, subagent_runner, subagent_grant
        )
        self.presenter.runtime = self.runtime
        self.runtime.on_run_started = self._show_run_started
        self.runtime.on_run_finished = self._show_run_finished
        self.runtime.on_run_interrupted = self._show_run_interrupted
        self.active_session = None


    def run(self, request: UserRequest) -> ExecutionSummary:
        return self._run_once(request)

    def run_background(self, request: UserRequest) -> ExecutionSummary:
        return self.runtime.run_background(request)

    def _validate_subagent_grant(self, request: UserRequest) -> None:
        return self.runtime._validate_subagent_grant(request)

    def chat(
        self,
        cwd: str,
        initial_prompt: str | None = None,
        conversation: list[dict[str, str]] | None = None,
        session_id: str | None = None,
        context_paths: list[str] | None = None,
    ) -> None:
        conversation = list(conversation or [])
        session = None
        resumed = bool(conversation) or session_id is not None
        session = self.runtime.open_session(cwd, conversation, session_id)
        if session is not None:
            self.presenter.show_session_state(session, resumed=resumed, engine=self.runtime.view())
            if resumed and hasattr(self.presenter, "show_session_history"):
                self._show_session_history(session)
        self.active_session = session


        prompt = initial_prompt
        context_generation_applied = getattr(session, "context_generation", 0)

        while True:
            session = self.active_session
            if prompt is None:

                try:
                    prompt = self.presenter.prompt_input(engine=self.runtime.view())
                except KeyboardInterrupt:
                    print()
                    self._close_session(session, conversation)
                    return
                except EOFError:
                    print()
                    self._close_session(session, conversation)
                    return

            if not prompt:
                prompt = None
                continue

            # Refresh before commands as well as model requests. A slash command
            # must not mark an old in-memory conversation as current.
            if session is not None and self.session_store is not None:
                latest_session = self.runtime.load_session(session.session_id)
                if (
                    latest_session is not None
                    and latest_session.context_generation > context_generation_applied
                ):
                    session.messages = list(latest_session.messages)
                    session.compaction_archive_ids = list(latest_session.compaction_archive_ids)
                    session.context_generation = latest_session.context_generation
                    session.context_trace_after_run_id = latest_session.context_trace_after_run_id
                    session.resume_state = latest_session.resume_state
                    session.trace = list(latest_session.trace)
                    conversation.clear()
                    conversation.extend(session.messages)
                    context_generation_applied = latest_session.context_generation

            if prompt.lower() in {"exit", "quit"}:
                self._close_session(session, conversation)
                return

            if prompt.startswith("/") or prompt in {"?", "？"}:
                should_exit = self.command_registry.execute(
                    self,
                    prompt,
                    session=session,
                    conversation=conversation,
                )
                if should_exit:
                    self._close_session(session, conversation)
                    return
                context_generation_applied = getattr(session, "context_generation", 0)
                prompt = None
                continue

            stable_request = (
                f"Current working directory: {self._workspace_root(session, cwd)}\n"
                f"User request: {prompt}"
            )
            if session is not None and self.session_store is not None:
                if self._auto_compact_if_needed(
                    session, conversation, stable_request
                ):
                    context_generation_applied = session.context_generation
                    continue

            request = self.runtime.build_session_request(prompt, cwd, session, conversation, context_paths)
            request_context_generation = getattr(session, "context_generation", 0)
            if not self.runtime.prepare_turn(session, request, request_context_generation, conversation):
                context_generation_applied = getattr(session, "context_generation", 0)
                continue
            try:
                summary = self._execute_context_turn(
                    request,
                    prompt,
                    session,
                    conversation,
                    request_context_generation,
                )
                if summary is None:
                    context_generation_applied = getattr(session, "context_generation", 0)
                    self.runtime.discard_prepared_turn(request)
                    continue
            except KeyboardInterrupt:
                self.runtime.save_interrupted_session(session)
                prompt = None
                continue
            prompt = None

    def _auto_compact_if_needed(
        self,
        session: StoredSession,
        conversation: list[dict[str, str]],
        pending_request_content: str,
    ) -> bool:
        if not self.runtime.compaction_needed(conversation, pending_request_content):
            return False

        if not self._confirm_compaction(
            "The conversation is approaching its context or storage budget."
        ):
            return False

        return self._compact_for_next_request(session, conversation)

    def _confirm_compaction(self, reason: str) -> bool:
        presenter = self.presenter
        if presenter is None or not hasattr(presenter, "prompt_box"):
            return False
        if hasattr(presenter, "_print"):
            presenter._print(f"\n[SaiWorks] {reason}")
        choice = presenter.prompt_box.read_selection(
            engine=self.runtime.view(),
            prompt="Choose an option [1-2]: ",
            options=("Compact now", "Continue without compacting"),
        )
        return str(choice or "").strip().lower() in {"1", "compact now", "yes", "y"}

    def _compact_for_next_request(
        self,
        session: StoredSession,
        conversation: list[dict[str, str]],
    ) -> bool:
        from .commands.session_cmds import handle_compact

        previous_generation = session.context_generation
        handle_compact(
            self,
            [],
            session=session,
            conversation=conversation,
        )
        return session.context_generation > previous_generation

    def _execute_context_turn(self, request, prompt, session, conversation, context_generation):
        return self.runtime.execute_session_turn(request, prompt, session, conversation, context_generation)

    def _append_turn_and_persist(
        self, request, prompt, summary, session, conversation, context_generation,
    ) -> None:
        previous_chars = sum(len(str(item.get("content", ""))) for item in session.messages) if session else 0
        self.runtime.append_turn_and_persist(request, prompt, summary, session, conversation, context_generation)
        stored_chars = sum(len(str(item.get("content", ""))) for item in session.messages) if session else 0
        if (self.session_store is not None and previous_chars <= self.session_store.max_message_chars < stored_chars
                and self.presenter and hasattr(self.presenter, "_print")):
            self.presenter._print(
                "\n[SaiWorks] Session storage budget exceeded. The complete latest turn "
                "was saved; choose whether to compact before the next request.\n"
            )

    def list_sessions(self) -> list[SessionRecord]:
        return self.runtime.list_sessions()

    def load_session(self, session_id: str) -> StoredSession | None:
        return self.runtime.load_session(session_id)

    def prepare_session_runtime(self, session: StoredSession | None) -> None:
        return self.runtime.prepare_session_runtime(session)

    def latest_session(self) -> StoredSession | None:
        return self.runtime.latest_session()

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
        self.runtime.persist_run(session, prompt, summary, model_user_content=model_user_content, status=status, close_runtime=close_runtime)
        if status == "closed" and self.session_store is not None:
            self._print_exit_info(session)

    _apply_workspace_summary = staticmethod(Runtime._apply_workspace_summary)

    _workspace_root = staticmethod(Runtime._workspace_root)

    _ensure_workspace_state = staticmethod(Runtime._ensure_workspace_state)

    _cluster_state_for_outcome = staticmethod(Runtime._cluster_state_for_outcome)

    def choose_session(self, prompt: str = "Select a session to resume") -> StoredSession | None:
        sessions = self.list_sessions()
        if not sessions:
            if self.presenter and hasattr(self.presenter, "_print"):
                self.presenter._print("\nNo saved sessions found.\n")
            return None

        import os
        import sys
        import select
        import tty
        import termios
        import signal

        if not sys.stdin.isatty() or not sys.stdout.isatty():
            self.presenter.show_session_list(sessions)
            while True:
                raw = input(f"{prompt} (Enter to cancel): ").strip()
                if not raw:
                    return None
                if not raw.isdigit():
                    print("[SaiWorks] enter a session number")
                    continue
                index = int(raw)
                if 1 <= index <= len(sessions):
                    return self.load_session(sessions[index - 1].session_id)
                print(f"[SaiWorks] choose a number between 1 and {len(sessions)}")

        fd = sys.stdin.fileno()
        previous_settings = termios.tcgetattr(fd)
        selected = 0
        max_visible = 3
        window_start = 0
        rendered_lines = 0

        def read_key() -> str:
            raw_key = os.read(fd, 1)
            if raw_key != b"\x1b":
                while True:
                    try:
                        return raw_key.decode()
                    except UnicodeDecodeError:
                        raw_key += os.read(fd, 1)
            sequence = "\x1b"
            for _ in range(32):
                readable, _, _ = select.select([fd], [], [], 0.05)
                if not readable:
                    break
                char = os.read(fd, 1).decode(errors="ignore")
                sequence += char
                if len(sequence) == 2 and char not in {"[", "O"}:
                    break
                if len(sequence) >= 3 and "@" <= char <= "~":
                    break
            return sequence

        import unicodedata

        def display_width(value: str) -> int:
            w = 0
            for character in value:
                if unicodedata.combining(character):
                    continue
                w += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
            return w

        def truncate_to_width(value: str, max_w: int) -> str:
            if display_width(value) <= max_w:
                return value
            current_w = 0
            truncated = []
            for character in value:
                char_w = 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
                if current_w + char_w > max_w - 3:
                    break
                truncated.append(character)
                current_w += char_w
            return "".join(truncated) + "..."

        def render_frame():
            nonlocal rendered_lines
            lines = []
            CYAN = "\033[1;36m"
            GREEN = "\033[1;32m"
            YELLOW = "\033[1;33m"
            GRAY = "\033[90m"
            RESET = "\033[0m"
            BOLD = "\033[1m"

            from .terminal import terminal_columns

            term_w = max(40, terminal_columns())
            max_line_len = term_w - 8

            header_text = f"{prompt} (use ↑/↓ keys, Enter to confirm, Esc to cancel):"
            if display_width(header_text) > max_line_len:
                header_text = f"{prompt} (↑/↓ to select, Enter/Esc):"
                if display_width(header_text) > max_line_len:
                    header_text = truncate_to_width(header_text, max_line_len)
            lines.append(f"{BOLD}{header_text}{RESET}")

            total = len(sessions)
            nonlocal window_start
            if selected >= window_start + max_visible:
                window_start = selected - max_visible + 1
            elif selected < window_start:
                window_start = selected
            window_start = max(0, min(window_start, total - max_visible))

            visible_sessions = sessions[window_start : window_start + max_visible]

            if window_start > 0:
                lines.append(f"  {GRAY}▲ ({window_start} more sessions above){RESET}")

            for rel_idx, s in enumerate(visible_sessions):
                actual_idx = window_start + rel_idx
                is_selected = actual_idx == selected

                status_colored = f"{GREEN}[active]{RESET}" if s.status == "active" else f"{GRAY}[closed]{RESET}"
                msg_count = s.message_count if hasattr(s, "message_count") else 0
                updated = s.updated_at if hasattr(s, "updated_at") else "N/A"
                preview = s.preview or "(no messages yet)"

                display_id = s.session_id
                if len(display_id) > 20:
                    display_id = display_id[:8] + "..." + display_id[-8:]

                # Safe title line length calculation & dynamic CJK-aware shortening
                raw_title_len = display_width(display_id) + display_width(s.status) + display_width(updated) + 16
                if raw_title_len > max_line_len:
                    if len(updated) > 10:
                        updated = updated[:10]  # Just YYYY-MM-DD
                    raw_title_len = display_width(display_id) + display_width(s.status) + display_width(updated) + 16
                    if raw_title_len > max_line_len:
                        target_id_w = max(5, max_line_len - display_width(s.status) - display_width(updated) - 20)
                        display_id = truncate_to_width(display_id, target_id_w)

                detail_text = f"Path: {s.cwd} | Preview: \"{preview}\""
                if display_width(detail_text) > max_line_len:
                    detail_text = truncate_to_width(detail_text, max_line_len)

                if is_selected:
                    pointer = f"{CYAN}›{RESET}"
                    card_title = f"{CYAN}{BOLD}{display_id}{RESET} {status_colored} ({msg_count} msgs) · {YELLOW}{updated}{RESET}"
                    card_detail = f"    {BOLD}Path:{RESET} {detail_text[6:]}"
                else:
                    pointer = " "
                    card_title = f"{GRAY}{display_id}{RESET} {status_colored} ({msg_count} msgs) · {GRAY}{updated}{RESET}"
                    card_detail = f"    {GRAY}{detail_text}{RESET}"

                lines.append(f"  {pointer} {card_title}")
                lines.append(card_detail)



            remaining_below = total - (window_start + len(visible_sessions))
            if remaining_below > 0:
                lines.append(f"  {GRAY}▼ ({remaining_below} more sessions below){RESET}")

            if rendered_lines > 0:
                sys.stdout.write(f"\r\033[{rendered_lines}A\033[J")

            for line in lines:
                sys.stdout.write(f"\r\033[2K{line}\n")
            sys.stdout.flush()
            rendered_lines = len(lines)

        try:
            tty.setcbreak(fd)
            render_frame()
            while True:
                readable, _, _ = select.select([fd], [], [], 0.1)
                if not readable:
                    continue
                key = read_key()
                if key in {"\r", "\n"}:
                    if rendered_lines > 0:
                        sys.stdout.write(f"\r\033[{rendered_lines}A\033[J")
                        sys.stdout.flush()
                    return self.load_session(sessions[selected].session_id)
                if key in {"\x1b", "\x03"}:
                    if rendered_lines > 0:
                        sys.stdout.write(f"\r\033[{rendered_lines}A\033[J")
                        sys.stdout.flush()
                    return None
                if key in {"\x1b[A", "\x1bOA", "k"}:
                    selected = (selected - 1) % len(sessions)
                    render_frame()
                elif key in {"\x1b[B", "\x1bOB", "j"}:
                    selected = (selected + 1) % len(sessions)
                    render_frame()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous_settings)


    def handle_resume_command(
        self,
        target_id: str | None = None,
        current_session: StoredSession | None = None,
        conversation: list[dict[str, str]] | None = None,
    ) -> StoredSession | None:

        if self.session_store is None:
            if self.presenter and hasattr(self.presenter, "_print"):
                self.presenter._print("[SaiWorks] session store is not available")
            return None

        selected_session: StoredSession | None = None
        if target_id:
            selected_session = self.runtime.load_session(target_id)
            if selected_session is None:
                if self.presenter and hasattr(self.presenter, "_print"):
                    self.presenter._print(f"[SaiWorks] session '{target_id}' not found")
                return None
        else:
            selected_session = self.choose_session()

        if selected_session is not None:
            if current_session is not None and conversation is not None:
                current_session.messages = list(conversation)
                self.runtime.save_session(current_session)
            if conversation is not None:
                conversation.clear()
                conversation.extend(selected_session.messages)
            self.active_session = selected_session
            self.prepare_session_runtime(selected_session)
            if self.presenter:
                self.presenter.show_session_state(selected_session, resumed=True, engine=self.runtime.view())
                if hasattr(self.presenter, "show_session_history"):
                    self._show_session_history(selected_session)
        return selected_session

    def _show_session_history(self, session: StoredSession) -> None:
        messages: list[dict[str, str]] = []
        if self.session_store is not None:
            for archive_id in session.compaction_archive_ids:
                archived = self.runtime.load_archived_messages(
                    session.session_id, archive_id
                )
                if archived is None:
                    continue
                messages.append({
                    "role": "system",
                    "content": f"Archived history: {archive_id} (view with /history {archive_id})",
                })
                messages.extend(archived)
        messages.extend(session.messages)
        self.presenter.show_session_history(messages)




    def _run_once(self, request: UserRequest) -> ExecutionSummary:
        return self.runtime.execute(request)

    def _show_run_started(self, request) -> None:
        self.presenter.show_start(request)
        self.presenter.show_status_bar(engine=self.runtime.view(), is_running=True)

    def _show_run_finished(self, summary) -> None:
        self.presenter.clear_running_status_bar(len(summary.tool_results))
        self.presenter.show_summary(summary)

    def _show_run_interrupted(self, summary) -> None:
        self.presenter.clear_running_status_bar(len(summary.tool_results))
        self.presenter.show_interrupted()
        if self.active_session is not None:
            self._apply_workspace_summary(self.active_session, summary)

    def _close_session(self, session, conversation: list[dict[str, str]]) -> None:
        self.runtime.close_session(session, conversation)
        self._print_exit_info(session)

    def _print_exit_info(self, session: StoredSession | None) -> None:
        usage = self.runtime.session_usage(session)
        prompt_tokens, completion_tokens = usage["prompt_tokens"], usage["completion_tokens"]
        total_tokens, has_usage = usage["total_tokens"], usage["has_usage"]

        CYAN = "\033[1;36m"
        GREEN = "\033[1;32m"
        YELLOW = "\033[1;33m"
        GRAY = "\033[90m"
        RESET = "\033[0m"
        BOLD = "\033[1m"

        print(f"{BOLD}Session closed successfully.{RESET}")
        
        if has_usage:
            print(f" {GRAY}›{RESET} {BOLD}Token Usage Summary:{RESET}")
            print(f"   {GREEN}•{RESET} Prompt Tokens:     {YELLOW}{prompt_tokens}{RESET}")
            print(f"   {GREEN}•{RESET} Completion Tokens: {YELLOW}{completion_tokens}{RESET}")
            print(f"   {GREEN}•{RESET} Total Tokens:      {YELLOW}{total_tokens}{RESET}")
        
        if session is not None:
            print(f" {GRAY}›{RESET} {BOLD}To resume this conversation, run:{RESET}")
            print(f"   {CYAN}sai-works --resume {session.session_id}{RESET}")
        print(f" {GRAY}›{RESET} {BOLD}To resume the most recent conversation, run:{RESET}")
        print(f"   {CYAN}sai-works --last{RESET}")
        print()

    def _attach_last_run_id(self, session: StoredSession) -> None:
        return self.runtime._attach_last_run_id(session)
