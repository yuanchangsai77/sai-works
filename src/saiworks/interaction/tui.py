from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from io import StringIO
import errno
import json
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import time
import unicodedata
from uuid import uuid4

from .presenter import ConsolePresenter
from .input import PasteInput, TerminalInput
from .terminal import Ansi
from .tui_events import TUIEvent, TUIEventKind, TUIEventQueue
from .tui_state import RunStatus, TUIState, ToolStatus, reduce_tui_state


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
    return width


def _visible_input_character(character: str) -> str:
    if character == "\n":
        return character
    if character == "\t":
        return r"\t"
    code = ord(character)
    if code < 32 or 127 <= code <= 159:
        return f"\\x{code:02x}"
    return character


def _visible_input_text(value: str) -> str:
    return "".join(_visible_input_character(character) for character in value)


def _truncate_display(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(value) <= width:
        return value
    if width == 1:
        return "…"
    result = ""
    used = 0
    for character in value:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in {"F", "W"} else 1
        )
        if used + character_width > width - 1:
            break
        result += character
        used += character_width
    return f"{result}…"


def _terminal_size(output=None) -> os.terminal_size:
    output = output or sys.stdout
    try:
        return os.get_terminal_size(output.fileno())
    except (AttributeError, NotImplementedError, OSError, ValueError):
        return shutil.get_terminal_size(fallback=(80, 24))


class TUIController:
    """Own the bounded event queue and the immutable runtime view state."""

    def __init__(self, max_events: int = 256) -> None:
        self.events = TUIEventQueue(max_events=max_events)
        self._state = TUIState()
        self._lock = threading.RLock()

    def publish(self, event: TUIEvent) -> None:
        self.events.publish(event)

    def drain(self) -> TUIState:
        with self._lock:
            for event in self.events.drain():
                self._state = reduce_tui_state(self._state, event)
            return self._state

    def snapshot(self) -> TUIState:
        with self._lock:
            return self._state


class TUIRenderer:
    spinner_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    styles = {
        "class:working": f"{Ansi.BOLD}",
        "class:runtime": f"{Ansi.YELLOW}",
        "class:tool.running": f"{Ansi.YELLOW}",
        "class:tool.succeeded": f"{Ansi.GREEN}",
        "class:tool.failed": f"{Ansi.RED}",
        "class:tool.skipped": f"{Ansi.YELLOW}",
        "class:approval.title": f"{Ansi.YELLOW}{Ansi.BOLD}",
        "class:approval": "",
        "class:approval.choice": f"{Ansi.BOLD}",
        "class:approval.hint": f"{Ansi.GRAY}",
    }

    def render(self, state: TUIState, *, now: float | None = None) -> str:
        return "\n".join(text for _style, text in self.render_rows(state, now=now))

    def render_rows(
        self,
        state: TUIState,
        *,
        now: float | None = None,
        include_runtime: bool = True,
    ) -> list[tuple[str, str]]:
        now = time.monotonic() if now is None else now
        width = max(1, state.terminal_width)
        rows: list[tuple[str, str]] = []

        running_tools = [tool for tool in state.tools if tool.status == ToolStatus.RUNNING]
        for tool in running_tools[-6:]:
            style = "class:tool.running"
            rows.append((style, self._fit(f" • {tool.name} → {tool.summary}", width)))

        if state.approval is not None:
            approval = state.approval
            rows.extend(
                [
                    (
                        "class:approval.title",
                        self._fit(f" Permission required: {approval.action_name}", width),
                    ),
                    ("class:approval", self._fit(f" {approval.reason}", width)),
                ]
            )
            if approval.arguments:
                rows.append(("class:approval", self._fit(f" {approval.arguments}", width)))
            yes = "› Yes" if approval.selected == 0 else "  Yes"
            no = "› No" if approval.selected == 1 else "  No"
            rows.extend(
                [
                    ("class:approval.choice", self._fit(f" {yes}", width)),
                    ("class:approval.choice", self._fit(f" {no}", width)),
                    (
                        "class:approval.hint",
                        self._fit(" ↑/↓ to select · enter to confirm · esc to deny", width),
                    ),
                ]
            )
            return rows

        has_model_stream = bool(state.model_stream_thinking or state.model_stream_message)
        if has_model_stream and state.model_stream_needs_separator:
            rows.append(("", ""))

        stream_budget = max(state.terminal_height - len(rows) - 8, 4)
        if state.model_stream_thinking:
            all_thinking_lines = state.model_stream_thinking.splitlines() or [""]
            thinking_budget = min(max(stream_budget // 3, 2), len(all_thinking_lines))
            thinking_lines = all_thinking_lines[-thinking_budget:]
            rows.append(("class:runtime", self._fit(" › thinking:", width)))
            if len(all_thinking_lines) > len(thinking_lines):
                rows.append(("class:runtime", self._fit("   …", width)))
            rows.extend(
                ("class:runtime", self._fit(f"   {line}", width))
                for line in thinking_lines
            )
            stream_budget = max(stream_budget - len(thinking_lines) - 1, 2)
        if state.model_stream_message:
            all_message_lines = state.model_stream_message.splitlines() or [""]
            message_lines = all_message_lines[-stream_budget:]
            if len(all_message_lines) > len(message_lines):
                rows.append(("", self._fit("   …", width)))
            rows.extend(
                ("", self._fit(f"   {line}", width))
                for line in message_lines
            )

        elapsed = state.elapsed(now)
        if state.run_status == RunStatus.CANCELLING:
            activity = "Cancelling"
        elif state.model_status:
            activity = state.model_status.rstrip("…")
        else:
            activity = "Working"
        spinner = self.spinner_frames[int(now * 10) % len(self.spinner_frames)]
        rows.append(
            (
                "class:working",
                self._fit(
                    f" {spinner} {activity} ({self._duration(elapsed)} • esc to interrupt)",
                    width,
                ),
            )
        )
        if include_runtime:
            runtime = self.runtime_row(state)
            if runtime is not None:
                rows.append(runtime)
        return rows

    def runtime_row(self, state: TUIState) -> tuple[str, str] | None:
        runtime = " · ".join(part for part in (state.model_name, state.cwd) if part)
        if not runtime:
            return None
        return "class:runtime", self._fit(f"  {runtime}", max(1, state.terminal_width))

    def ansi_row(self, style: str, text: str) -> str:
        prefix = self.styles.get(style, "")
        return f"{prefix}{text}{Ansi.RESET}" if prefix else text

    @staticmethod
    def _duration(seconds: float) -> str:
        total = max(0, int(seconds))
        minutes, seconds = divmod(total, 60)
        return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    @staticmethod
    def _fit(value: str, width: int) -> str:
        safe_width = max(width - 1, 1)
        if _display_width(value) <= safe_width:
            return value
        if safe_width <= 1:
            return "…"
        kept: list[str] = []
        used = 0
        for character in value:
            character_width = max(_display_width(character), 0)
            if used + character_width > safe_width - 1:
                break
            kept.append(character)
            used += character_width
        return "".join(kept) + "…"


from .commands import SlashCommandRegistry, default_slash_command_registry


def _normalize_key(key: str) -> str:
    """Translate supported CSI-u/modifyOtherKeys keys to existing operations."""
    if isinstance(key, PasteInput):
        return key
    match = re.fullmatch(r"\x1b\[(\d+)(?:;(\d+))?u", key)
    if match:
        code = int(match[1])
        modifiers = int(match[2] or "1") - 1
    else:
        match = re.fullmatch(r"\x1b\[27;(\d+);(\d+)~", key)
        if not match:
            return key
        code = int(match[2])
        modifiers = int(match[1]) - 1
    # Lock flags do not change the operation of these control keys.
    modifiers &= ~(64 | 128)
    if code == 57414:  # Keypad Enter is distinct under keyboard enhancement.
        code = 13
    if modifiers == 0:
        return {
            27: "\x1b", 13: "\r", 9: "\t", 127: "\x7f",
            57417: "\x1b[D", 57418: "\x1b[C", 57419: "\x1b[A", 57420: "\x1b[B",
            57423: "\x1b[H", 57424: "\x1b[F",
        }.get(code, key)
    if code == 13 and modifiers in {1, 2}:  # Shift+Enter / Alt+Enter
        return "\x1b\r"
    if modifiers == 4 and (65 <= code <= 90 or 97 <= code <= 122):
        return chr(code & 31)
    return key


@dataclass
class ComposerState:
    value: str = ""
    cursor: int = 0
    history_index: int | None = None
    saved_value: str = ""
    completion_index: int = 0
    completion_dismissed: bool = False
    command_registry: SlashCommandRegistry | None = None
    command_context: object | None = None

    def set_value(self, value: str) -> None:
        self.value = value
        self.cursor = len(value)
        self.history_index = None
        self.saved_value = ""
        self.completion_index = 0
        self.completion_dismissed = False

    def insert(self, value: str) -> None:
        self.value = self.value[: self.cursor] + value + self.value[self.cursor :]
        self.cursor += len(value)
        self.history_index = None
        self.completion_index = 0
        self.completion_dismissed = False

    def get_completion_matches(self) -> list[tuple[str, str]]:
        if self.completion_dismissed or not self.value.startswith("/"):
            return []
        registry = self.command_registry or default_slash_command_registry()
        return registry.get_completions(self.value, self.command_context)


    def edit(self, key: str, history: list[str]) -> str | None:
        if isinstance(key, PasteInput):
            self.insert(str(key).replace("\r\n", "\n").replace("\r", "\n"))
            return "changed"
        key = _normalize_key(key)
        matches = self.get_completion_matches()
        if matches:
            if self.completion_index >= len(matches):
                self.completion_index = 0
            if key in {"\x1b[A", "\x1bOA"}:
                self.completion_index = (self.completion_index - 1) % len(matches)
                return "changed"
            if key in {"\x1b[B", "\x1bOB"}:
                self.completion_index = (self.completion_index + 1) % len(matches)
                return "changed"
            if key == "\t":
                selected_cmd = matches[self.completion_index][0]
                self.value = selected_cmd + " "
                self.cursor = len(self.value)
                self.completion_index = 0
                return "changed"
            if key in {"\r", "\n"}:
                selected_cmd = matches[self.completion_index][0]
                if self.value != selected_cmd:
                    self.value = selected_cmd
                    self.cursor = len(self.value)
                    self.completion_index = 0
                    if self._advance_to_argument_options(selected_cmd):
                        return "changed"
                    return "changed"
                if self._advance_to_argument_options(selected_cmd):
                    return "changed"
                return "submit"
            if key == "\x1b":
                self.completion_dismissed = True
                return "changed"

        if key in {"\r", "\n"}:
            return "submit"
        if key in {"\x1b\r", "\x1b\n"}:
            self.insert("\n")
            return "changed"
        if key in {"\x7f", "\b"}:
            if self.cursor:
                self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
                self.cursor -= 1
                self.history_index = None
                self.completion_index = 0
                self.completion_dismissed = False
                return "changed"
            return None
        if key == "\x04":
            if not self.value:
                return "eof"
            if self.cursor < len(self.value):
                self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
                self.completion_index = 0
                self.completion_dismissed = False
                return "changed"
            return None
        if key in {"\x1b[D", "\x1bOD"}:
            if self.cursor:
                self.cursor -= 1
                return "changed"
            return None
        if key in {"\x1b[C", "\x1bOC"}:
            if self.cursor < len(self.value):
                self.cursor += 1
                return "changed"
            return None
        if key in {"\x1b[H", "\x1bOH", "\x01"}:
            self.cursor = 0
            return "changed"
        if key in {"\x1b[F", "\x1bOF", "\x05"}:
            self.cursor = len(self.value)
            return "changed"
        if key in {"\x1b[A", "\x1bOA"}:
            return self._move_history(history, -1)
        if key in {"\x1b[B", "\x1bOB"}:
            return self._move_history(history, 1)
        if key and not key.startswith("\x1b") and all(
            character.isprintable() or character in {"\n", "\t"}
            for character in key
        ):
            self.insert(key)
            return "changed"
        return None

    def _advance_to_argument_options(self, selected: str) -> bool:
        parts = selected.split()
        if not parts:
            return False
        should_advance = len(parts) == 1 or (
            len(parts) == 2
            and parts[0] == "/capabilities"
            and parts[1] in {"open", "activate"}
        ) or (
            len(parts) == 3
            and parts[:2] == ["/capabilities", "activate"]
            and parts[2].startswith("--scope=")
        )
        if not should_advance:
            return False
        registry = self.command_registry or default_slash_command_registry()
        next_value = f"{selected} "
        if not registry.get_completions(next_value, self.command_context):
            return False
        self.value = next_value
        self.cursor = len(self.value)
        self.completion_index = 0
        self.completion_dismissed = False
        return True

    def _move_history(self, history: list[str], direction: int) -> str | None:
        if not history:
            return None
        if self.history_index is None:
            if direction > 0:
                return None
            self.saved_value = self.value
            self.history_index = len(history) - 1
        else:
            next_index = self.history_index + direction
            if next_index >= len(history):
                self.set_value(self.saved_value)
                return "changed"
            self.history_index = max(next_index, 0)
        self.value = history[self.history_index]
        self.cursor = len(self.value)
        return "changed"



@dataclass
class _UserInputWaiter:
    draft: ComposerState
    completed: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


class InlineTerminalSurface:
    """Redraw the transient tail directly after committed scrollback output."""

    def __init__(self, output=None) -> None:
        self.output = output or sys.stdout
        self._lock = threading.RLock()
        self._active = False
        self._rows: list[str] = []
        self._cursor_row = 0
        self._cursor_column = 0

    def render(self, rows: list[str], *, cursor_row: int, cursor_column: int) -> None:
        with self._lock:
            self._clear_locked()
            if not rows:
                return
            self.output.write("\033[?25l")
            self.output.write("\n".join(rows))
            rows_up = len(rows) - 1 - cursor_row
            self.output.write("\r")
            if rows_up:
                self.output.write(f"\033[{rows_up}A")
            if cursor_column:
                self.output.write(f"\033[{cursor_column}C")
            self.output.write("\033[?25h")
            self.output.flush()
            self._active = True
            self._rows = list(rows)
            self._cursor_row = cursor_row
            self._cursor_column = cursor_column

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()
            self.output.flush()

    def commit(self, rows: list[str]) -> None:
        """Move stable rows into native scrollback without racing a redraw."""
        if not rows:
            return
        with self._lock:
            self._clear_locked()
            for row in rows:
                print(row, file=self.output)
            self.output.flush()

    def _clear_locked(self) -> None:
        if not self._active:
            return
        size = _terminal_size(self.output)
        visual_cursor_row = self._cursor_visual_row(size.columns)
        self.output.write("\r")
        if visual_cursor_row:
            self.output.write(f"\033[{visual_cursor_row}A")
        self.output.write("\r\033[J")
        self._active = False
        self._rows = []
        self._cursor_row = 0
        self._cursor_column = 0

    def _cursor_visual_row(self, columns: int) -> int:
        columns = max(columns, 1)
        rows_before = self._visual_height(self._rows[: self._cursor_row], columns)
        return rows_before + self._cursor_column // columns

    @staticmethod
    def _visual_height(rows: list[str], columns: int) -> int:
        columns = max(columns, 1)
        height = 0
        for row in rows:
            plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", row)
            width = max(_display_width(plain), 1)
            height += max((width + columns - 1) // columns, 1)
        return height


class TUIConsolePresenter(ConsolePresenter):
    """Normal-screen TUI with native scrollback and a transient inline tail."""

    manages_runtime_display = True
    refresh_interval = 0.1

    def __init__(self, tool_result_summarizer=None, *, input=None, output=None) -> None:
        super().__init__(tool_result_summarizer=tool_result_summarizer)
        self.controller = TUIController()
        self.renderer = TUIRenderer()
        self._input = input
        self._output = output or sys.stdout
        self._surface = InlineTerminalSurface(self._output)
        self._history: list[str] = []
        self._composer = ComposerState()
        self._composer_draft = ""
        self._queued_prompt: str | None = None
        self._pending_prompt = ""
        self._pending_worked_seconds: float | None = None
        self._cwd = os.getcwd()
        self._runtime_active = False
        self._runtime_stop = threading.Event()
        self._runtime_render_thread: threading.Thread | None = None
        self._runtime_input_thread: threading.Thread | None = None
        self._approval_waiters: dict[str, tuple[threading.Event, list[bool]]] = {}
        self._approval_lock = threading.Lock()
        self._input_lock = threading.RLock()
        self._user_input_waiter: _UserInputWaiter | None = None
        self._input_bytes: deque[int] = deque()
        self._key_bytes = bytearray()
        self._paste_bytes: bytearray | None = None
        self._pending_keys: deque[str] = deque()
        self._flushed_tool_ids: set[str] = set()
        self._active_tool_names: dict[str, str] = {}
        self._stable_tail_has_separator = False
        self._tui_model_streams: dict[str, dict[str, list[str]]] = {}
        self._tui_model_replies: dict[str, tuple[str, bool]] = {}
        self._reader_lock = threading.RLock()
        self.prompt_box.set_terminal_input(TerminalInput(
            read_key=lambda: self._read_key(),
            ready=lambda timeout: self._input_ready(timeout),
            mode=self._selection_input_mode,
            available=lambda: self._can_use_native_input(),
        ))

    @contextmanager
    def _selection_input_mode(self):
        if self._runtime_active:
            raise RuntimeError("Running selections must use the runtime input dispatcher")
        with self._raw_input():
            yield

    def clear_screen(self) -> None:
        """Clear the terminal and redraw the TUI idle frame."""
        self._surface.clear()
        self._output.write("\033[H\033[2J\033[3J")
        self._output.flush()
        if getattr(self, "_session", None) is not None:
            super().show_session_state(self._session, resumed=getattr(self, "_resumed", False), engine=getattr(self, "_engine", None))
        engine = getattr(self, "_engine", None)
        if self._can_use_native_input():
            self._render_idle(engine=engine)

    def _print(self, value: str = "") -> None:
        self._surface.clear()
        print(value, file=self._output)

    def _print_many(self, values: list[str]) -> None:
        self._surface.clear()
        for value in values:
            print(value, file=self._output)

    def show_session_state(self, session, resumed: bool, engine=None) -> None:
        self._cwd = session.cwd
        super().show_session_state(session, resumed=resumed, engine=engine)

    def show_user_prompt(self, prompt: str) -> None:
        lines = self.prompt_box.wrap_prompt_value(_visible_input_text(prompt))
        prompt_lbl = f" {Ansi.CYAN}SaiWorks>{Ansi.RESET}"
        background = "\033[48;5;236m"
        blank = f"{background}\033[K{Ansi.RESET}"
        self._print(blank)
        self._print(f"{background}{prompt_lbl}{background} {lines[0]}\033[K{Ansi.RESET}")
        for line in lines[1:]:
            self._print(f"{background}  {line}\033[K{Ansi.RESET}")
        self._print(blank)
        self._print()
        self._stable_tail_has_separator = True

    def show_start(self, request) -> None:
        self._pending_prompt = request.prompt
        self._cwd = request.cwd
        lines = self.prompt_box.wrap_prompt_value(_visible_input_text(request.prompt))
        prompt = f" {Ansi.CYAN}SaiWorks>{Ansi.RESET}"
        background = "\033[48;5;236m"
        blank = f"{background}\033[K{Ansi.RESET}"
        self._print(blank)
        self._print(f"{background}{prompt}{background} {lines[0]}\033[K{Ansi.RESET}")
        for line in lines[1:]:
            self._print(f"{background}  {line}\033[K{Ansi.RESET}")
        self._print(blank)
        self._print()
        self._stable_tail_has_separator = True

    def prompt_input(self, engine=None) -> str:
        self._show_worked_separator()
        if self._queued_prompt is not None:
            prompt = self._queued_prompt
            self._queued_prompt = None
            return prompt
        if not self._can_use_native_input():
            return super().prompt_input(engine=engine)

        self._composer.set_value(self._composer_draft)
        self._composer_draft = ""
        try:
            with self._raw_input():
                last_size: os.terminal_size | None = None
                while True:
                    size = _terminal_size(self._output)
                    if size != last_size:
                        self._render_idle(engine)
                        last_size = size
                    if not self._input_ready(timeout=0.1):
                        continue
                    key = self._read_key()
                    if key is None:
                        continue
                    result = self._composer.edit(key, self._history)
                    if not isinstance(key, PasteInput) and key == "\x03":
                        raise KeyboardInterrupt
                    if result == "eof":
                        raise EOFError
                    if result == "submit":
                        with self._input_lock:
                            value = self._composer.value.strip()
                            self._composer.set_value("")
                            self._composer_draft = ""
                        self._surface.clear()
                        if value:
                            self._history.append(value)
                        return value
                    if result == "changed":
                        self._render_idle(engine)
                        last_size = _terminal_size(self._output)
        finally:
            self._surface.clear()

    def request_user_input(self, question: str, options: list[str]):
        if not self._runtime_active or not self._can_use_native_input():
            return super().request_user_input(question, options)
        # The runtime input thread remains the sole terminal reader. The
        # synchronous Runtime adapter owns replying/cancelling the durable wait.
        waiter = None
        try:
            with self._input_lock:
                if self._user_input_waiter is not None:
                    raise RuntimeError("Already waiting for a user reply")
                waiter = _UserInputWaiter(draft=replace(self._composer))
                self._user_input_waiter = waiter
                self._composer.set_value("")
            self._print(question)
            if options:
                self._print('Suggested answers: ' + ' / '.join(options))
            self._print('Leave blank to cancel this task.')
            while not waiter.completed.wait(0.05):
                if not self._runtime_active:
                    return None
            return waiter.answer
        finally:
            if waiter is not None:
                self._resolve_user_input(waiter)

    def _resolve_user_input(self, waiter: _UserInputWaiter, answer: str | None = None) -> None:
        with self._input_lock:
            if self._user_input_waiter is not waiter:
                return
            waiter.answer = answer
            self._composer = waiter.draft
            self._user_input_waiter = None
            waiter.completed.set()

    def show_status_bar(self, engine=None, active_tasks_count=0, is_running=False, left_override=None) -> None:
        if not is_running:
            return super().show_status_bar(
                engine=engine,
                active_tasks_count=active_tasks_count,
                is_running=is_running,
                left_override=left_override,
            )
        model_name = getattr(getattr(engine, "model", None), "model", "StubModel")
        self.controller.publish(
            TUIEvent(
                TUIEventKind.RUN_STARTED,
                payload={
                    "prompt": self._pending_prompt,
                    "model_name": model_name,
                    "cwd": self._cwd,
                },
            )
        )
        self._start_runtime()

    def clear_running_status_bar(self, tools_count: int) -> None:
        self._stop_runtime(TUIEventKind.RUN_FINISHED)
        self._flush_tool_transcript()

    def show_run_resumed(self) -> None:
        self._active_tool_names.clear()
        self._publish(TUIEvent(TUIEventKind.RUN_RESUMED))

    def show_interrupted(self) -> None:
        self._stop_runtime(TUIEventKind.RUN_FAILED)
        super().show_interrupted()

    def model_started(self, message: str = "Model is thinking…") -> str:
        self._last_streamed_thinking = ""
        self._last_streamed_message = ""
        handle = f"model-{uuid4().hex}"
        self._tui_model_streams[handle] = {"message": [], "thinking": []}
        self._publish(
            TUIEvent(
                TUIEventKind.MODEL_STARTED,
                entity_id=handle,
                payload={
                    "message": message,
                    "needs_separator": not self._stable_tail_has_separator,
                },
            )
        )
        return handle

    def model_response_ready(self, handle: str, message: str, done: bool) -> None:
        """Classify a validated reply before its transient preview is finalized."""
        if handle in self._tui_model_streams:
            self._tui_model_replies[handle] = (message, done)

    def model_finished(self, handle: str) -> None:
        self._publish(TUIEvent(TUIEventKind.MODEL_FINISHED, entity_id=handle))
        stream = self._tui_model_streams.pop(handle, None)
        reply = self._tui_model_replies.pop(handle, None)
        if stream is None or reply is None:
            return
        thinking = "".join(stream["thinking"])
        message, done = reply
        if done:
            # The complete final answer is rendered after the runtime surface and
            # tool transcript have been committed. Do not suppress that render.
            self._last_streamed_thinking = ""
            self._last_streamed_message = ""
            return
        rows: list[str] = []
        if thinking:
            rows.append(f" {Ansi.GRAY}› thinking:{Ansi.RESET}")
            rows.extend(
                f"{Ansi.GRAY}   {line}{Ansi.RESET}"
                for line in thinking.splitlines()
            )
        if message and message != "Model requested tool calls.":
            rows.extend(self._render_stable_markdown_rows(message))
        if rows:
            self._surface.commit([*rows, ""])
            self._stable_tail_has_separator = True

    def model_stream_delta(self, handle: str, channel: str, text: str) -> None:
        stream = self._tui_model_streams.get(handle)
        if stream is None or channel not in stream or not text:
            return
        stream[channel].append(text)
        self._publish(
            TUIEvent(
                TUIEventKind.MODEL_STREAM_DELTA,
                entity_id=handle,
                payload={
                    "message": "".join(stream["message"])[-32_000:],
                    "thinking": "".join(stream["thinking"])[-8_000:],
                },
            )
        )

    def model_retrying(self, handle, retry, max_retries, status, delay_seconds) -> None:
        had_partial = bool(
            handle in self._tui_model_streams
            and any(self._tui_model_streams[handle].values())
        )
        if handle in self._tui_model_streams:
            self._tui_model_streams[handle] = {"message": [], "thinking": []}
        self._tui_model_replies.pop(handle, None)
        msg = f"{status} — retrying {retry}/{max_retries} in {delay_seconds:g}s…" if delay_seconds > 0 else f"{status} — retrying {retry}/{max_retries}…"
        if had_partial:
            msg = f"Partial response discarded. {msg}"
        self._publish(
            TUIEvent(
                TUIEventKind.MODEL_RETRYING,
                entity_id=handle,
                payload={
                    "message": msg
                },
            )
        )

    def tool_started(self, action_name: str) -> str:
        handle = f"tool-{uuid4().hex}"
        self._active_tool_names[handle] = action_name
        self._publish(
            TUIEvent(TUIEventKind.TOOL_STARTED, entity_id=handle, payload={"name": action_name})
        )
        return handle

    def tool_finished(self, handle: str, action, result) -> None:
        summary = self._summarize_tool_result(result)
        status = ToolStatus.SUCCEEDED if result.success else ToolStatus.FAILED
        name = self._active_tool_names.pop(handle, action.name)
        self._publish(
            TUIEvent(
                TUIEventKind.TOOL_FINISHED,
                entity_id=handle,
                payload={
                    "name": name,
                    "success": result.success,
                    "summary": summary,
                },
            )
        )
        self._commit_tool_row(handle, name, status, summary)

    def tool_aborted(self, handle: str) -> None:
        name = self._active_tool_names.pop(handle, "tool")
        self._publish(
            TUIEvent(
                TUIEventKind.TOOL_ABORTED,
                entity_id=handle,
                payload={"summary": "aborted"},
            )
        )
        self._commit_tool_row(handle, name, ToolStatus.ABORTED, "aborted")

    def tool_skipped(self, action, reason: str) -> None:
        handle = f"tool-{uuid4().hex}"
        self._publish(
            TUIEvent(
                TUIEventKind.TOOL_SKIPPED,
                entity_id=handle,
                payload={"name": action.name, "summary": reason},
            )
        )
        self._commit_tool_row(handle, action.name, ToolStatus.SKIPPED, reason)

    def confirm_tool_action(self, action, reason: str) -> bool:
        if not self._runtime_active:
            return super().confirm_tool_action(action, reason)
        approval_id = f"approval-{uuid4().hex}"
        completed = threading.Event()
        result: list[bool] = []
        with self._approval_lock:
            self._approval_waiters[approval_id] = completed, result
        arguments = json.dumps(action.arguments, ensure_ascii=False) if action.arguments else ""
        self._publish(
            TUIEvent(
                TUIEventKind.APPROVAL_REQUESTED,
                entity_id=approval_id,
                payload={
                    "action_name": action.name,
                    "reason": reason,
                    "arguments": arguments,
                },
            )
        )
        while not completed.wait(0.1):
            if not self._runtime_active:
                with self._approval_lock:
                    self._approval_waiters.pop(approval_id, None)
                return False
        return bool(result and result[0])

    def _start_runtime(self) -> None:
        if self._runtime_active:
            return
        self._runtime_active = True
        self._runtime_stop.clear()
        with self._input_lock:
            self._composer.set_value(self._composer_draft)
            self._composer_draft = ""
        self._runtime_render_thread = threading.Thread(
            target=self._runtime_render_loop,
            name="saiworks-tui-render",
            daemon=True,
        )
        self._runtime_render_thread.start()
        if self._can_use_native_input():
            self._runtime_input_thread = threading.Thread(
                target=self._runtime_input_loop,
                name="saiworks-tui-input",
                daemon=True,
            )
            self._runtime_input_thread.start()

    def _stop_runtime(self, outcome: TUIEventKind) -> None:
        if not self._runtime_active:
            return
        state = self.controller.drain()
        if state.run_started_at is not None:
            self._pending_worked_seconds = state.elapsed()
        with self._input_lock:
            self._runtime_active = False
            self._runtime_stop.set()
            if self._user_input_waiter is not None:
                self._resolve_user_input(self._user_input_waiter)
            if self._queued_prompt is None:
                self._composer_draft = self._composer.value
        self.controller.publish(TUIEvent(outcome))
        for thread in (self._runtime_input_thread, self._runtime_render_thread):
            if thread is not None and thread.is_alive():
                # Input reads poll rather than block on a complete key/paste.
                # Never transfer stdin while its previous reader is alive.
                thread.join(timeout=None if thread is self._runtime_input_thread else 1.0)
        self._surface.clear()
        self.controller.drain()
        self._runtime_input_thread = None
        self._runtime_render_thread = None
        self._deny_all_approvals()

    def _runtime_render_loop(self) -> None:
        while not self._runtime_stop.is_set():
            self._render_runtime()
            self._runtime_stop.wait(self.refresh_interval)

    def _runtime_input_loop(self) -> None:
        try:
            with self._raw_input():
                while not self._runtime_stop.is_set():
                    if not self._input_ready(timeout=0.05):
                        continue
                    key = self._read_key(stop_event=self._runtime_stop)
                    if key is None:
                        continue
                    with self._input_lock:
                        if self._runtime_stop.is_set():
                            self._pending_keys.appendleft(key)
                            return
                        state = self.controller.drain()
                        if state.approval is not None:
                            self._handle_approval_key(key, state)
                            continue
                        if not isinstance(key, PasteInput) and key in {"\x03", "\x1b"}:
                            if self._user_input_waiter is not None:
                                self._resolve_user_input(self._user_input_waiter)
                                interrupt = False
                            else:
                                interrupt = True
                        else:
                            result = self._composer.edit(key, self._history)
                            interrupt = result == "submit" and self._queue_runtime_prompt(state)
                    if interrupt:
                        self._interrupt_runtime()
                        return
        except EOFError:
            if not self._runtime_stop.is_set():
                self._interrupt_runtime()

    def _handle_approval_key(self, key: str, state: TUIState) -> None:
        approval = state.approval
        if isinstance(key, PasteInput):
            return
        if approval is None:
            return
        if key in {"\x03", "\x1b", "n", "N"}:
            self._resolve_approval(approval.approval_id, False)
        elif key in {"y", "Y"}:
            self._resolve_approval(approval.approval_id, True)
        elif key in {"\r", "\n"}:
            self._resolve_approval(approval.approval_id, approval.selected == 0)
        elif key in {"\x1b[A", "\x1bOA", "\x1b[D", "\x1bOD"}:
            self._select_approval(approval.approval_id, approval.selected - 1)
        elif key in {"\x1b[B", "\x1bOB", "\x1b[C", "\x1bOC"}:
            self._select_approval(approval.approval_id, approval.selected + 1)

    def _submit_runtime_prompt(self, state: TUIState | None = None) -> bool:
        interrupt = self._queue_runtime_prompt(state)
        if interrupt:
            self._interrupt_runtime()
        return interrupt

    def _queue_runtime_prompt(self, state: TUIState | None = None) -> bool:
        state = state or self.controller.drain()
        with self._input_lock:
            if state.approval is not None:
                return False
            prompt = self._composer.value.strip()
            parts = prompt.split()
            if parts and parts[0].lower() == "/mode":
                # Harness control is independent of the running task or reply.
                runtime = getattr(self, "runtime", None)
                if runtime is not None:
                    mode_arg = parts[1].lower() if len(parts) > 1 else None
                    self.show_or_change_mode(runtime.view(), mode_arg)
                    self._composer.set_value("")
                    self._composer_draft = ""
                    return False
            if self._user_input_waiter is not None:
                self._resolve_user_input(self._user_input_waiter, prompt or None)
                return False
            if state.run_status != RunStatus.WORKING or not prompt:
                return False
            self._queued_prompt = prompt
            self._composer.set_value("")
            self._composer_draft = ""
            return True

    def _interrupt_runtime(self) -> None:
        self._publish(TUIEvent(TUIEventKind.RUN_CANCELLING))
        os.kill(os.getpid(), signal.SIGINT)

    def _render_idle(self, engine=None) -> None:
        size = _terminal_size(self._output)
        composer_rows, cursor_row, cursor_column = self._composer_rows(size.columns)
        blank = f"\033[48;5;236m\033[K{Ansi.RESET}"
        status = self.status_bar.text(engine=engine, is_running=False)
        matches = self._composer.get_completion_matches()
        completion_rows = self._completion_rows(matches)
        rows = [blank, *composer_rows, blank, *completion_rows, f"{Ansi.GRAY}{status}{Ansi.RESET}"]
        self._surface.render(rows, cursor_row=1 + cursor_row, cursor_column=cursor_column)

    def _render_runtime(self) -> None:
        size = _terminal_size(self._output)
        self.controller.publish(
            TUIEvent(
                TUIEventKind.RESIZED,
                payload={"width": size.columns, "height": size.lines},
            )
        )
        state = self.controller.drain()
        rows, cursor_row, cursor_column = self._runtime_frame(state, size.columns)
        self._surface.render(
            rows,
            cursor_row=cursor_row,
            cursor_column=cursor_column,
        )

    def _runtime_frame(
        self,
        state: TUIState,
        columns: int,
    ) -> tuple[list[str], int, int]:
        with self._input_lock:
            activity = [
                self.renderer.ansi_row(style, text)
                for style, text in self.renderer.render_rows(state, include_runtime=False)
            ]
            composer_rows, cursor_row, cursor_column = self._composer_rows(columns)
            blank = f"\033[48;5;236m\033[K{Ansi.RESET}"
            runtime = self.renderer.runtime_row(state)
            metadata = "" if runtime is None else self.renderer.ansi_row(*runtime)
            if self._user_input_waiter is not None:
                metadata = f"{Ansi.GRAY}Waiting for your reply · Enter to reply · esc to cancel{Ansi.RESET}"
            matches = self._composer.get_completion_matches()
            completion_rows = self._completion_rows(matches)
            rows = [*activity, "", blank, *composer_rows, blank, *completion_rows, metadata]
            return (
                rows,
                len(activity) + 2 + cursor_row,
                cursor_column,
            )

    def _composer_rows(self, columns: int) -> tuple[list[str], int, int]:
        prompt = " SaiWorks> "
        plain_lines, cursor_row, cursor_column, start = self._wrap_composer(columns)
        rows: list[str] = []
        background = "\033[48;5;236m"
        for index, line in enumerate(plain_lines):
            real_index = start + index
            prefix = (
                f"{Ansi.CYAN}{prompt}{Ansi.RESET}{background}"
                if real_index == 0
                else "  "
            )
            rows.append(f"{background}{prefix}{line}\033[K{Ansi.RESET}")
        real_cursor_row = start + cursor_row
        if real_cursor_row == 0:
            cursor_column += _display_width(prompt)
        else:
            cursor_column += 2
        return rows, cursor_row, cursor_column

    def _completion_rows(self, matches: list[tuple[str, str]], max_visible: int = 5) -> list[str]:
        if not matches:
            return []

        total = len(matches)
        selected_idx = min(self._composer.completion_index, total - 1)

        window_start = 0
        if total > max_visible:
            if selected_idx >= max_visible:
                window_start = min(selected_idx - max_visible + 1, total - max_visible)
            window_start = max(0, window_start)

        visible_matches = matches[window_start : window_start + max_visible]

        menu_name = "Options" if " " in self._composer.value else "Commands"
        header = f"  {Ansi.GRAY}{menu_name} ({total} total):{Ansi.RESET}"
        if total > max_visible:
            header += f" {Ansi.GRAY}({window_start + 1}-{window_start + len(visible_matches)} of {total}){Ansi.RESET}"
        rows: list[str] = [header]

        if window_start > 0:
            rows.append(f"   {Ansi.GRAY}▲ ({window_start} more above){Ansi.RESET}")

        display_matches = [
            (cmd.rsplit(" ", 1)[-1] if menu_name == "Options" else cmd, desc)
            for cmd, desc in matches
        ]
        visible_display_matches = display_matches[
            window_start : window_start + max_visible
        ]
        columns = max(_terminal_size(self._output).columns, 1)
        natural_cmd_width = max(_display_width(cmd) for cmd, _ in display_matches)
        max_cmd_width = max(columns - 5 - 2 - 16 - 1, 8)
        cmd_width = min(natural_cmd_width, max_cmd_width)
        description_width = max(columns - 5 - cmd_width - 2 - 1, 0)
        for rel_idx, (cmd, desc) in enumerate(visible_display_matches):
            actual_idx = window_start + rel_idx
            is_selected = actual_idx == selected_idx
            pointer = "›" if is_selected else " "
            cmd = _truncate_display(cmd, cmd_width)
            cmd_padding = " " * max(cmd_width - _display_width(cmd), 0)
            desc = _truncate_display(desc, description_width)
            if is_selected:
                cmd_str = f"{Ansi.CYAN}{Ansi.BOLD}{cmd}{cmd_padding}{Ansi.RESET}"
                desc_str = f"{Ansi.YELLOW}{desc}{Ansi.RESET}"
                pointer_str = f"{Ansi.CYAN}{Ansi.BOLD}{pointer}{Ansi.RESET}"
            else:
                cmd_str = f"{Ansi.GRAY}{cmd}{cmd_padding}{Ansi.RESET}"
                desc_str = f"{Ansi.GRAY}{desc}{Ansi.RESET}"
                pointer_str = f"{Ansi.GRAY}{pointer}{Ansi.RESET}"
            separator = "  " if desc else ""
            rows.append(f"   {pointer_str} {cmd_str}{separator}{desc_str}")

        remaining_below = total - (window_start + len(visible_matches))
        if remaining_below > 0:
            rows.append(f"   {Ansi.GRAY}▼ ({remaining_below} more below){Ansi.RESET}")

        return rows




    def _wrap_composer(self, columns: int) -> tuple[list[str], int, int, int]:
        columns = max(columns, 2)
        prompt_width = _display_width(" SaiWorks> ")
        lines = [""]
        positions: list[tuple[int, int]] = [(0, 0)]
        row = 0
        used = 0
        limit = max(columns - prompt_width - 1, 1)
        for character in self._composer.value:
            if character == "\n":
                row += 1
                lines.append("")
                used = 0
                limit = max(columns - 3, 1)
                positions.append((row, used))
                continue
            for visible in _visible_input_character(character):
                width = max(_display_width(visible), 1)
                if used + width > limit and lines[-1]:
                    row += 1
                    lines.append("")
                    used = 0
                    limit = max(columns - 3, 1)
                lines[-1] += visible
                used += width
            positions.append((row, used))
        cursor_index = min(self._composer.cursor, len(positions) - 1)
        cursor_row, cursor_column = positions[cursor_index]
        start = 0
        max_rows = 6
        if len(lines) > max_rows:
            start = max(min(cursor_row - (max_rows - 1), len(lines) - max_rows), 0)
            lines = lines[start : start + max_rows]
            cursor_row -= start
        return lines, cursor_row, cursor_column, start

    def _show_worked_separator(self) -> None:
        if self._pending_worked_seconds is None:
            return
        columns = max(_terminal_size(self._output).columns, 1)
        label = f"─ Worked for {self.renderer._duration(self._pending_worked_seconds)} "
        line = label + "─" * max(columns - _display_width(label), 0)
        self._print(f"{Ansi.GRAY}{line}{Ansi.RESET}")
        self._print()
        self._pending_worked_seconds = None

    def _flush_tool_transcript(self) -> None:
        state = self.controller.snapshot()
        flushed_any = False
        for tool in state.tools:
            if tool.tool_id in self._flushed_tool_ids:
                continue
            style = {
                ToolStatus.RUNNING: "class:tool.running",
                ToolStatus.SUCCEEDED: "class:tool.succeeded",
                ToolStatus.FAILED: "class:tool.failed",
                ToolStatus.ABORTED: "class:tool.failed",
                ToolStatus.SKIPPED: "class:tool.skipped",
            }[tool.status]
            self._print(self.renderer.ansi_row(style, f" • {tool.name} → {tool.summary}"))
            self._flushed_tool_ids.add(tool.tool_id)
            self._stable_tail_has_separator = False
            flushed_any = True
        if flushed_any:
            self._print()

    def _commit_tool_row(
        self,
        handle: str,
        name: str,
        status: ToolStatus,
        summary: str,
    ) -> None:
        if handle in self._flushed_tool_ids:
            return
        style = {
            ToolStatus.RUNNING: "class:tool.running",
            ToolStatus.SUCCEEDED: "class:tool.succeeded",
            ToolStatus.FAILED: "class:tool.failed",
            ToolStatus.ABORTED: "class:tool.failed",
            ToolStatus.SKIPPED: "class:tool.skipped",
        }[status]
        row = self.renderer.ansi_row(style, f" • {name} → {summary}")
        self._surface.commit([row])
        self._flushed_tool_ids.add(handle)
        self._stable_tail_has_separator = False

    def _markdown_renderable(self, text: str, *, top_padding: int | None = None):
        if top_padding is None:
            top_padding = 0 if self._stable_tail_has_separator else 1
        self._stable_tail_has_separator = False
        return super()._markdown_renderable(text, top_padding=top_padding)

    def _render_stable_markdown_rows(self, text: str) -> list[str]:
        """Render a validated model turn off-screen before an atomic commit."""
        try:
            from rich.console import Console

            buffer = StringIO()
            output_is_terminal = bool(
                getattr(self._output, "isatty", lambda: False)()
            )
            console = Console(
                file=buffer,
                width=max(_terminal_size(self._output).columns, 1),
                force_terminal=output_is_terminal,
            )
            console.print(self._markdown_renderable(text))
            return buffer.getvalue().rstrip("\n").splitlines()
        except ImportError:
            return [f"   {line}" for line in text.splitlines()]

    def _render_dim_markdown_rows(self, text: str) -> list[str]:
        try:
            from rich.console import Console

            buffer = StringIO()
            output_is_terminal = bool(
                getattr(self._output, "isatty", lambda: False)()
            )
            console = Console(
                file=buffer,
                width=max(_terminal_size(self._output).columns, 1),
                force_terminal=output_is_terminal,
            )
            console.print(self._markdown_renderable(text, top_padding=0), style="dim")
            return [*buffer.getvalue().rstrip("\n").splitlines(), ""]
        except ImportError:
            return [*[f"\033[90m   {line}\033[0m" for line in text.splitlines()], ""]

    def _publish(self, event: TUIEvent) -> None:
        self.controller.publish(event)

    def _select_approval(self, approval_id: str, selected: int) -> None:
        self._publish(
            TUIEvent(
                TUIEventKind.APPROVAL_SELECTED,
                entity_id=approval_id,
                payload={"selected": selected},
            )
        )

    def _resolve_approval(self, approval_id: str, approved: bool) -> None:
        with self._approval_lock:
            waiter = self._approval_waiters.pop(approval_id, None)
        if waiter is None:
            return
        self._publish(TUIEvent(TUIEventKind.APPROVAL_RESOLVED, entity_id=approval_id))
        completed, result = waiter
        result.append(approved)
        completed.set()

    def _deny_all_approvals(self) -> None:
        with self._approval_lock:
            waiters = list(self._approval_waiters.values())
            self._approval_waiters.clear()
        for completed, result in waiters:
            result.append(False)
            completed.set()

    def _can_use_native_input(self) -> bool:
        if self._input is not None:
            return True
        return sys.stdin.isatty() and self._output.isatty()

    def _input_fd(self) -> int:
        source = self._input or sys.stdin
        return source if isinstance(source, int) else source.fileno()

    def _input_ready(self, timeout: float | None = None) -> bool:
        if self._pending_keys or self._input_bytes:
            return True
        readable, _, _ = select.select([self._input_fd()], [], [], timeout)
        return bool(readable)

    class _RawInputContext:
        def __init__(self, presenter: "TUIConsolePresenter") -> None:
            self.presenter = presenter
            self.settings = None
            self.fd = None

        def __enter__(self):
            self.presenter._reader_lock.acquire()
            try:
                self.fd = self.presenter._input_fd()
                try:
                    self.settings = termios.tcgetattr(self.fd)
                except (OSError, termios.error) as error:
                    if error.args[0] != errno.ENOTTY:
                        raise
                if self.settings is not None:
                    settings = list(self.settings)
                    settings[6] = list(self.settings[6])
                    # Preserve output processing, but deliver input bytes to
                    # the decoder before signals, flow control, or CR mapping.
                    settings[0] &= ~(termios.BRKINT | termios.IGNBRK | termios.ICRNL | termios.INLCR
                                     | termios.IGNCR | termios.INPCK | termios.ISTRIP | termios.PARMRK
                                     | termios.IXON | termios.IXOFF | termios.IXANY)
                    settings[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
                    settings[6][termios.VMIN] = 1
                    settings[6][termios.VTIME] = 0
                    termios.tcsetattr(self.fd, termios.TCSANOW, settings)
                self.presenter._output.write("\033[?2004h\033[>1u")
                self.presenter._output.flush()
            except BaseException:
                try:
                    self._restore()
                finally:
                    self.presenter._reader_lock.release()
                raise
            return self

        def _restore(self):
            if self.settings is not None:
                try:
                    termios.tcsetattr(self.fd, termios.TCSADRAIN, self.settings)
                except (OSError, termios.error):
                    pass

        def __exit__(self, _type, _value, _traceback):
            try:
                self.presenter._output.write("\033[<u\033[?2004l")
                self.presenter._output.flush()
            finally:
                try:
                    self._restore()
                finally:
                    self.presenter._reader_lock.release()

    def _raw_input(self):
        return self._RawInputContext(self)

    def _read_input_byte(
        self, fd: int, *, stop_event: threading.Event | None = None, timeout: float = 0.0,
    ) -> bytes | None:
        if stop_event is not None and stop_event.is_set():
            return None
        if self._input_bytes:
            return bytes([self._input_bytes.popleft()])
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            return None
        chunk = os.read(fd, 4096)
        if not chunk:
            return b""
        self._input_bytes.extend(chunk[1:])
        return chunk[:1]

    def _read_key(self, *, stop_event: threading.Event | None = None) -> str | None:
        """Poll one event, retaining incomplete input across reader handoffs."""
        if self._pending_keys:
            return self._pending_keys.popleft()
        fd = self._input_fd()
        if self._paste_bytes is not None:
            return self._read_paste(fd, stop_event=stop_event)
        raw = self._key_bytes
        while True:
            key = None
            if raw and (raw[0] != 27 or (len(raw) > 1 and raw[1] not in {91, 79})):
                try:
                    key = raw.decode()
                except UnicodeDecodeError as error:
                    if error.reason != "unexpected end of data":
                        key = raw.decode(errors="replace")
            elif len(raw) >= 3 and (64 <= raw[-1] <= 126 or len(raw) >= 33):
                key = raw.decode(errors="replace")
            if key is not None:
                raw.clear()
                if key == "\x1b[200~":
                    self._paste_bytes = bytearray()
                    return self._read_paste(fd, stop_event=stop_event)
                return _normalize_key(key)
            chunk = self._read_input_byte(
                fd, stop_event=stop_event, timeout=0.02 if raw == b"\x1b" else 0.0,
            )
            if chunk is None:
                if raw == b"\x1b":
                    raw.clear()
                    return "\x1b"
                return None
            if not chunk:
                if raw:
                    key = raw.decode(errors="replace")
                    raw.clear()
                    return _normalize_key(key)
                raise EOFError
            raw.extend(chunk)

    def _read_paste(self, fd: int, *, stop_event: threading.Event | None = None) -> PasteInput | None:
        terminator = b"\x1b[201~"
        if self._paste_bytes is None:
            self._paste_bytes = bytearray()
        payload = self._paste_bytes
        while not payload.endswith(terminator):
            chunk = self._read_input_byte(fd, stop_event=stop_event)
            if chunk is None:
                return None
            if not chunk:
                break
            payload.extend(chunk)
        if payload.endswith(terminator):
            del payload[-len(terminator) :]
        self._paste_bytes = None
        return PasteInput(payload.decode(errors="replace"))


def should_use_tui() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
