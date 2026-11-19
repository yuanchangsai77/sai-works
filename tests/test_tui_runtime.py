from __future__ import annotations

from contextlib import contextmanager
from io import StringIO
import os
import re
import signal
from types import SimpleNamespace
import threading
import time

import pytest

from saiworks.interaction.tui import (
    ComposerState,
    InlineTerminalSurface,
    TUIConsolePresenter,
    TUIController,
    TUIRenderer,
    _display_width,
)
from saiworks.interaction.tui_events import TUIEvent, TUIEventKind, TUIEventQueue
from saiworks.interaction.tui_state import RunStatus, ToolStatus
from saiworks.types import ExecutionSummary


def _plain(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def test_event_queue_coalesces_refresh_events_but_keeps_lifecycle_events():
    events = TUIEventQueue(max_events=8)
    events.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": 40}))
    events.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": 120}))
    for index in range(7):
        events.publish(
            TUIEvent(TUIEventKind.TOOL_STARTED, entity_id=str(index), payload={"name": "tool"})
        )

    drained = events.drain()

    resize_events = [event for event in drained if event.kind == TUIEventKind.RESIZED]
    assert [event.payload["width"] for event in resize_events] == [120]
    assert len([event for event in drained if event.kind == TUIEventKind.TOOL_STARTED]) == 7


def test_event_queue_applies_backpressure_instead_of_dropping_outcomes():
    events = TUIEventQueue(max_events=8)
    for index in range(8):
        events.publish(TUIEvent(TUIEventKind.TOOL_STARTED, entity_id=str(index)))
    published = threading.Event()
    producer = threading.Thread(
        target=lambda: (
            events.publish(TUIEvent(TUIEventKind.RUN_FINISHED)),
            published.set(),
        )
    )
    producer.start()
    time.sleep(0.02)
    assert published.is_set() is False

    first_batch = events.drain()
    producer.join(timeout=1)

    assert len(first_batch) == 8
    assert published.is_set() is True
    assert [event.kind for event in events.drain()] == [TUIEventKind.RUN_FINISHED]


def test_controller_reduces_runtime_events_by_stable_tool_id():
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"prompt": "检查项目", "model_name": "gpt-5", "cwd": "/repo"},
        )
    )
    controller.publish(
        TUIEvent(TUIEventKind.TOOL_STARTED, entity_id="tool-1", payload={"name": "read_file"})
    )
    controller.publish(
        TUIEvent(
            TUIEventKind.TOOL_FINISHED,
            entity_id="tool-1",
            payload={"success": True, "summary": "README.md"},
        )
    )

    state = controller.drain()

    assert state.run_status == RunStatus.WORKING
    assert state.request_summary == "检查项目"
    assert state.model_name == "gpt-5"
    assert state.cwd == "/repo"
    assert state.tools[0].status == ToolStatus.SUCCEEDED


@pytest.mark.parametrize("width", [20, 40, 80, 120])
def test_renderer_respects_terminal_width_for_chinese_content(width):
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"prompt": "分析问题", "model_name": "gpt-5", "cwd": "/很长的路径"},
        )
    )
    controller.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": width, "height": 20}))

    frame = TUIRenderer().render(controller.drain(), now=time.monotonic() + 2)

    assert frame
    assert all(_display_width(line) < width or width == 1 for line in frame.splitlines())


def test_renderer_keeps_model_metadata_separate_from_activity():
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"model_name": "gpt-5", "cwd": "/repo"},
        )
    )
    state = controller.drain()
    renderer = TUIRenderer()

    activity = renderer.render_rows(state, now=0, include_runtime=False)

    assert "gpt-5" not in "".join(text for _style, text in activity)
    assert renderer.runtime_row(state) == ("class:runtime", "  gpt-5 · /repo")


def test_renderer_shows_complete_pending_model_stream_instead_of_last_three_lines():
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    controller.publish(TUIEvent(TUIEventKind.MODEL_STARTED, entity_id="model-1"))
    controller.publish(
        TUIEvent(
            TUIEventKind.MODEL_STREAM_DELTA,
            entity_id="model-1",
            payload={
                "thinking": "checking",
                "message": "first\nsecond\nthird\nfourth",
            },
        )
    )

    frame = _plain(TUIRenderer().render(controller.drain()))

    assert "thinking:" in frame
    assert "checking" in frame
    assert "   first" in frame
    assert "   second" in frame
    assert "   third" in frame
    assert "   fourth" in frame
    assert "Receiving model stream" in frame


def test_renderer_uses_terminal_height_for_sliding_stream_preview():
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    controller.publish(TUIEvent(TUIEventKind.RESIZED, payload={"width": 80, "height": 14}))
    controller.publish(
        TUIEvent(
            TUIEventKind.MODEL_STREAM_DELTA,
            entity_id="model-1",
            payload={"message": "\n".join(f"line-{index}" for index in range(10))},
        )
    )

    frame = _plain(TUIRenderer().render(controller.drain()))

    assert "line-0" not in frame
    assert "line-9" in frame
    assert "   …" in frame


def test_tui_stream_previews_incrementally_then_defers_final_answer_to_renderer(monkeypatch):
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    committed: list[str] = []
    monkeypatch.setattr(presenter._surface, "commit", lambda rows: committed.extend(rows))

    handle = presenter.model_started()
    presenter.model_stream_delta(handle, "message", "您好！\n有什么可以")

    state = presenter.controller.drain()
    assert committed == []
    assert state.model_stream_message == "您好！\n有什么可以"
    assert "   您好！" in _plain(TUIRenderer().render(state))
    assert "   有什么可以" in _plain(TUIRenderer().render(state))

    presenter.model_stream_delta(handle, "message", "帮您的吗？")
    presenter.model_response_ready(
        handle,
        "您好！\n有什么可以帮您的吗？",
        True,
    )
    presenter.model_finished(handle)

    assert committed == []
    assert presenter._last_streamed_message == ""
    presenter.show_summary(
        ExecutionSummary(
            final_message="## 问候\n\n**您好！** 有什么可以帮您的吗？",
            tool_results=[],
        )
    )
    rendered = _plain(output.getvalue())
    assert "问候" in rendered
    assert "您好！" in rendered
    assert "**您好！**" not in rendered


def test_tui_renders_complete_tool_round_markdown_but_not_protocol_placeholder(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    committed: list[list[str]] = []
    monkeypatch.setattr(presenter._surface, "commit", lambda rows: committed.append(rows))

    handle = presenter.model_started()
    presenter.model_stream_delta(handle, "message", "## 读取计划")
    presenter.model_response_ready(handle, "## 读取计划\n\n- 架构文档", False)
    presenter.model_finished(handle)
    placeholder = presenter.model_started()
    presenter.model_response_ready(placeholder, "Model requested tool calls.", False)
    presenter.model_finished(placeholder)

    assert len(committed) == 1
    rendered = _plain("\n".join(committed[0]))
    assert committed[0][0].strip() == ""
    assert "读取计划" in rendered
    assert "架构文档" in rendered
    assert "##" not in rendered
    assert "Model requested tool calls." not in rendered


def test_tui_commits_model_turns_and_tools_in_causal_order(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    committed: list[str] = []
    monkeypatch.setattr(
        presenter._surface,
        "commit",
        lambda rows: committed.append(_plain("\n".join(rows))),
    )

    first_model = presenter.model_started()
    presenter.model_response_ready(first_model, "**第一轮**", False)
    presenter.model_finished(first_model)
    first_tool = presenter.tool_started("read_file")
    presenter.tool_finished(
        first_tool,
        SimpleNamespace(name="read_file"),
        SimpleNamespace(success=True, output="first.py", error_code=None),
    )
    second_model = presenter.model_started()
    presenter.model_response_ready(second_model, "**第二轮**", False)
    presenter.model_finished(second_model)
    second_tool = presenter.tool_started("search_text")
    presenter.tool_finished(
        second_tool,
        SimpleNamespace(name="search_text"),
        SimpleNamespace(success=True, output="4 matches", error_code=None),
    )

    assert len(committed) == 4
    assert committed[0].splitlines()[0].strip() == ""
    assert "第一轮" in committed[0]
    assert "read_file → first.py" in committed[1]
    assert "第二轮" in committed[2]
    assert "search_text → 4 matches" in committed[3]


def test_final_markdown_has_a_blank_line_after_the_last_tool():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    tool = presenter.tool_started("read_file")
    presenter.tool_finished(
        tool,
        SimpleNamespace(name="read_file"),
        SimpleNamespace(success=True, output="README.md", error_code=None),
    )

    presenter.show_summary(
        ExecutionSummary(final_message="项目架构整理如下：", tool_results=[])
    )

    lines = _plain(output.getvalue()).splitlines()
    tool_line = next(index for index, line in enumerate(lines) if "read_file → README.md" in line)
    answer_line = next(index for index, line in enumerate(lines) if "项目架构整理如下：" in line)
    assert answer_line >= tool_line + 2
    assert all(not line.strip() for line in lines[tool_line + 1 : answer_line])


def test_first_answer_reuses_prompt_separator_without_adding_a_second_blank_line():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    presenter.show_start(SimpleNamespace(prompt="您好", cwd="/repo"))

    presenter.show_summary(
        ExecutionSummary(final_message="您好！请问有什么可以帮您的吗？", tool_results=[])
    )

    lines = _plain(output.getvalue()).splitlines()
    prompt_line = next(index for index, line in enumerate(lines) if "SaiWorks> 您好" in line)
    answer_line = next(
        index for index, line in enumerate(lines) if "您好！请问有什么可以帮您的吗？" in line
    )
    # One blank row belongs to the input box itself; exactly one fixed blank
    # row remains between the bottom of that box and the answer.
    assert answer_line == prompt_line + 3
    assert all(not line.strip() for line in lines[prompt_line + 1 : answer_line])


def test_stream_preview_adds_missing_separator_immediately_after_a_tool(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    monkeypatch.setattr(presenter._surface, "commit", lambda _rows: None)
    tool = presenter.tool_started("list_dir")
    presenter.tool_finished(
        tool,
        SimpleNamespace(name="list_dir"),
        SimpleNamespace(success=True, output="listed /repo", error_code=None),
    )
    model = presenter.model_started()
    presenter.model_stream_delta(model, "message", "正在分析")

    rows = presenter.renderer.render_rows(presenter.controller.drain())
    message_index = next(index for index, (_style, text) in enumerate(rows) if "正在分析" in text)
    assert message_index > 0
    assert rows[message_index - 1][1] == ""


def test_stream_preview_reuses_fixed_prompt_separator_without_adding_another():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.show_start(SimpleNamespace(prompt="看看", cwd="/repo"))
    model = presenter.model_started()
    presenter.model_stream_delta(model, "message", "正在查看")

    state = presenter.controller.drain()
    rows = presenter.renderer.render_rows(state)
    message_index = next(index for index, (_style, text) in enumerate(rows) if "正在查看" in text)
    assert state.model_stream_needs_separator is False
    assert message_index == 0


def test_failed_tool_transcript_is_committed_before_rendered_final_answer():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    tool_handle = presenter.tool_started("toolbox_open")
    presenter.tool_finished(
        tool_handle,
        SimpleNamespace(name="toolbox_open"),
        SimpleNamespace(
            name="toolbox_open",
            success=False,
            output="unknown toolbox",
            error_code="capability_toolbox_unavailable",
        ),
    )
    presenter.controller.drain()

    final_handle = presenter.model_started()
    presenter.model_stream_delta(final_handle, "message", "## 最终架构")
    presenter.model_response_ready(final_handle, "## 最终架构", True)
    presenter.model_finished(final_handle)
    presenter.controller.drain()
    presenter._flush_tool_transcript()
    presenter.show_summary(
        ExecutionSummary(final_message="## 最终架构", tool_results=[])
    )

    rendered = _plain(output.getvalue())
    assert rendered.index("toolbox_open") < rendered.index("最终架构")
    assert "## 最终架构" not in rendered


def test_renderer_keeps_only_running_tools_in_transient_rows():
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    controller.publish(
        TUIEvent(TUIEventKind.TOOL_STARTED, entity_id="running", payload={"name": "read"})
    )
    controller.publish(
        TUIEvent(TUIEventKind.TOOL_STARTED, entity_id="done", payload={"name": "test"})
    )
    controller.publish(
        TUIEvent(
            TUIEventKind.TOOL_FINISHED,
            entity_id="done",
            payload={"success": True, "summary": "passed"},
        )
    )

    rows = TUIRenderer().render_rows(controller.drain())

    assert rows[0][0] == "class:tool.running"
    assert rows[0][1].startswith(" • read →")
    assert all("test → passed" not in text for _style, text in rows)


def test_tui_commits_each_terminal_tool_state_immediately(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    committed: list[list[str]] = []
    monkeypatch.setattr(
        presenter._surface,
        "commit",
        lambda rows: committed.append([_plain(row) for row in rows]),
    )

    finished = presenter.tool_started("list_dir")
    presenter.tool_finished(
        finished,
        SimpleNamespace(name="list_dir"),
        SimpleNamespace(success=True, output="listed /repo", error_code=None),
    )
    aborted = presenter.tool_started("read_file")
    presenter.tool_aborted(aborted)
    presenter.tool_skipped(SimpleNamespace(name="write_file"), "not approved")

    assert committed == [
        [" • list_dir → listed /repo"],
        [" • read_file → aborted"],
        [" • write_file → not approved"],
    ]
    state = presenter.controller.drain()
    assert all(tool.status != ToolStatus.RUNNING for tool in state.tools)
    assert all(tool.name not in _plain(TUIRenderer().render(state)) for tool in state.tools)

    presenter._flush_tool_transcript()
    assert len(committed) == 3


def test_composer_supports_unicode_multiline_cursor_and_history():
    composer = ComposerState()
    composer.edit("你好", [])
    composer.edit("\x1b\r", [])
    composer.edit("world", [])
    composer.edit("\x1b[D", [])
    composer.edit("!", [])

    assert composer.value == "你好\nworl!d"
    assert composer.edit("\r", []) == "submit"

    composer.set_value("draft")
    composer.edit("\x1b[A", ["first", "second"])
    assert composer.value == "second"
    composer.edit("\x1b[B", ["first", "second"])
    assert composer.value == "draft"


def test_inline_surface_redraws_only_transient_tail_without_alternate_screen_or_mouse():
    output = StringIO()
    surface = InlineTerminalSurface(output)

    surface.render(["working", "saiworks> hello", "model"], cursor_row=1, cursor_column=12)
    surface.render(["done", "saiworks> hello", "model"], cursor_row=1, cursor_column=12)
    surface.clear()

    rendered = output.getvalue()
    assert "working" in rendered and "done" in rendered
    assert "\x1b[?1049h" not in rendered
    assert "\x1b[?1000h" not in rendered
    assert "\x1b[J" in rendered


def test_inline_surface_accounts_for_old_frame_reflow_when_clearing(monkeypatch):
    output = StringIO()
    monkeypatch.setattr(
        "saiworks.interaction.tui._terminal_size",
        lambda output=None: os.terminal_size((10, 18)),
    )
    surface = InlineTerminalSurface(output)
    surface._active = True
    surface._rows = ["123456789012345", "input", "model"]
    surface._cursor_row = 1
    surface._cursor_column = 3

    surface.clear()

    assert "\r\033[2A\r\033[J" in output.getvalue()


def test_prompt_input_reads_utf8_without_prompt_toolkit():
    read_fd, write_fd = os.pipe()
    output = StringIO()
    try:
        os.write(write_fd, "你好，saiworks\r".encode())
        presenter = TUIConsolePresenter(input=read_fd, output=output)

        value = presenter.prompt_input()
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert value == "你好，saiworks"
    assert "\x1b[?1049h" not in output.getvalue()


def test_committed_output_is_written_once_to_native_scrollback():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)

    presenter.show_start(SimpleNamespace(prompt="inspect", cwd="/repo"))
    presenter._print("answer")

    plain = _plain(output.getvalue())
    assert plain.count("SaiWorks> inspect") == 1
    assert plain.count("answer") == 1


def test_worked_separator_has_a_blank_line_below(monkeypatch):
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    presenter._pending_worked_seconds = 220
    monkeypatch.setattr(
        "saiworks.interaction.tui._terminal_size",
        lambda output=None: os.terminal_size((60, 20)),
    )

    presenter._show_worked_separator()

    plain = _plain(output.getvalue())
    assert "Worked for 3m 40s" in plain
    assert plain.endswith("\n\n")


def test_runtime_frame_places_model_below_composer(monkeypatch):
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    presenter.controller.publish(
        TUIEvent(
            TUIEventKind.RUN_STARTED,
            payload={"model_name": "gpt-5", "cwd": "/repo"},
        )
    )
    monkeypatch.setattr(
        "saiworks.interaction.tui._terminal_size",
        lambda output=None: os.terminal_size((80, 24)),
    )

    presenter._render_runtime()

    plain = _plain(output.getvalue())
    assert plain.index("SaiWorks>") < plain.index("gpt-5 · /repo")


def test_runtime_thinking_has_plain_blank_rows_above_and_below():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    state = presenter.controller.drain()

    rows, _cursor_row, _cursor_column = presenter._runtime_frame(state, 80)
    plain_rows = [_plain(row) for row in rows]
    thinking_index = next(
        index for index, row in enumerate(plain_rows) if "Working" in row
    )
    assert thinking_index == 0
    assert plain_rows[thinking_index + 1] == ""


def test_prompt_text_reapplies_gray_background_after_colored_label():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("hello")

    rows, _cursor_row, _cursor_column = presenter._composer_rows(80)

    assert "SaiWorks> \033[0m\033[48;5;236mhello" in rows[0]
    assert "hello\033[K\033[0m" in rows[0]


def test_gray_input_rows_do_not_use_reflowable_space_padding():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("hello")
    rows, _cursor_row, _cursor_column = presenter._composer_rows(100)
    blank = "\033[48;5;236m\033[K\033[0m"

    assert " " * 20 not in rows[0]
    assert InlineTerminalSurface._visual_height([blank, rows[0]], 20) == 2


def test_runtime_submission_interrupts_and_queues_next_prompt(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    state = presenter.controller.drain()
    presenter._composer.set_value("change direction")
    signals = []
    monkeypatch.setattr(
        "saiworks.interaction.tui.os.kill",
        lambda process_id, sent_signal: signals.append((process_id, sent_signal)),
    )

    submitted = presenter._submit_runtime_prompt(state)

    assert submitted is True
    assert presenter._queued_prompt == "change direction"
    assert presenter.controller.drain().run_status == RunStatus.CANCELLING
    assert signals[0][1] == signal.SIGINT


def test_runtime_lifecycle_commits_stable_tool_result_without_fullscreen():
    output = StringIO()
    presenter = TUIConsolePresenter(output=output)
    engine = SimpleNamespace(model=SimpleNamespace(model="gpt-5"))
    presenter.show_start(SimpleNamespace(prompt="inspect", cwd="/repo"))

    presenter.show_status_bar(engine=engine, is_running=True)
    handle = presenter.tool_started("read_file")
    presenter.tool_finished(
        handle,
        SimpleNamespace(name="read_file"),
        SimpleNamespace(success=True, output="ok", error_code=None, name="read_file"),
    )
    presenter.clear_running_status_bar(1)

    state = presenter.controller.snapshot()
    plain = _plain(output.getvalue())
    assert presenter._runtime_active is False
    assert state.run_status == RunStatus.IDLE
    assert state.tools[0].status == ToolStatus.SUCCEEDED
    assert plain.count("read_file → ok") == 1
    assert "\x1b[?1049h" not in output.getvalue()


def test_approval_waits_for_runtime_resolution():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    decision: list[bool] = []
    worker = threading.Thread(
        target=lambda: decision.append(
            presenter.confirm_tool_action(
                SimpleNamespace(name="patch", arguments={"path": "README.md"}),
                "modify a file",
            )
        )
    )
    worker.start()

    deadline = time.monotonic() + 1
    approval = None
    while approval is None and time.monotonic() < deadline:
        approval = presenter.controller.drain().approval
        time.sleep(0.01)
    assert approval is not None

    presenter._resolve_approval(approval.approval_id, True)
    worker.join(timeout=1)

    assert decision == [True]


def test_renderer_formats_approval_choices_vertically():
    controller = TUIController()
    controller.publish(
        TUIEvent(
            TUIEventKind.APPROVAL_REQUESTED,
            entity_id="app-1",
            payload={
                "action_name": "shell_exec",
                "reason": "tool 'shell_exec' has risk 'execute' and requires explicit approval",
                "arguments": '{"command": "echo \\"hallo\\""}',
            },
        )
    )
    state = controller.drain()

    renderer = TUIRenderer()
    rows = renderer.render_rows(state)
    assert rows[3] == ("class:approval.choice", " › Yes")
    assert rows[4] == ("class:approval.choice", "   No")
    assert rows[5] == ("class:approval.hint", " ↑/↓ to select · enter to confirm · esc to deny")

    rendered = renderer.render(state)
    lines = rendered.splitlines()
    assert " Permission required: shell_exec" in lines[0]
    assert " › Yes" in lines[3]
    assert "   No" in lines[4]
    assert " ↑/↓ to select · enter to confirm · esc to deny" in lines[5]


def test_composer_rows_scrolled_multiline_does_not_repeat_prompt_prefix():
    presenter = TUIConsolePresenter(output=StringIO())
    long_input = "d" * 600
    presenter._composer.set_value(long_input)

    rows, _cursor_row, _cursor_column = presenter._composer_rows(80)
    plain_rows = [_plain(row) for row in rows]

    assert len(plain_rows) == 6
    assert plain_rows[0].startswith("  ")
    assert "SaiWorks>" not in plain_rows[0]
    assert all(len(row) < 80 for row in plain_rows)


def test_composer_slash_command_autocomplete_navigation_and_tab_fill():
    composer = ComposerState()
    composer.edit("/", [])

    matches = composer.get_completion_matches()
    cmd_names = [cmd for cmd, _ in matches]
    assert "/clear" in cmd_names and "/status" in cmd_names and "/resume" in cmd_names
    assert composer.completion_index == 0

    # Down arrow moves selection
    composer.edit("\x1b[B", [])
    assert composer.completion_index == 1

    # Filtering by prefix /sk
    composer.set_value("/sk")
    matches_sk = composer.get_completion_matches()
    assert [cmd for cmd, _ in matches_sk] == ["/skill", "/skills"]

    # Enter confirms the selected /skill command
    result = composer.edit("\r", [])
    assert result == "changed"
    assert composer.value == "/skill"

    # Subsequent enter submits
    result_submit = composer.edit("\r", [])
    assert result_submit == "submit"


def test_composer_rows_renders_slash_command_completion_menu():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("/")

    matches = presenter._composer.get_completion_matches()
    rows = presenter._completion_rows(matches)
    plain_rows = [_plain(row) for row in rows]

    assert any("Commands (" in row for row in plain_rows)
    assert any("/clear" in row for row in plain_rows)
    assert any("/compact" in row for row in plain_rows)
    assert any(f"▼ ({len(matches) - 5} more below)" in row for row in plain_rows)

    # Scroll selection down to /status
    status_idx = next(i for i, (cmd, _) in enumerate(matches) if cmd == "/status")
    presenter._composer.completion_index = status_idx
    scrolled_rows = presenter._completion_rows(matches)
    scrolled_plain = [_plain(row) for row in scrolled_rows]
    assert any("▲ (" in row for row in scrolled_plain)
    assert any("› /status" in row for row in scrolled_plain)


@pytest.mark.parametrize("columns", [50, 80, 120])
def test_completion_rows_never_wrap_long_capability_descriptions(monkeypatch, columns):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value("/capabilities activate ")
    monkeypatch.setattr(
        "saiworks.interaction.tui._terminal_size",
        lambda _output=None: os.terminal_size((columns, 24)),
    )
    matches = [
        (
            "local:subagents:subagent_spawn",
            "Create a new independent child session for a delegated task. "
            "Do not use this for feedback or repairs owned by another child.",
        ),
        (
            "local:subagents:subagent_run_ready",
            "Execute every ready child session concurrently with isolated model runtimes.",
        ),
    ]

    rows = presenter._completion_rows(matches)
    plain_rows = [_plain(row) for row in rows]

    assert all(_display_width(row) < columns for row in plain_rows)
    assert any(row.endswith("…") for row in plain_rows)


def test_composer_offers_dynamic_second_level_command_options(tmp_path, monkeypatch):
    from saiworks.app import create_app

    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    app = create_app(workspace_root=tmp_path)
    first_session = app.session_store.create(cwd=str(tmp_path), messages=[{"role": "user", "content": "first task"}])
    composer = ComposerState(
        command_registry=app.command_registry,
        command_context=app,
    )

    composer.set_value("/mode ")
    assert [value for value, _ in composer.get_completion_matches()] == [
        "/mode readonly",
        "/mode confirm",
        "/mode auto",
    ]
    composer.set_value("/mo")
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/mode "
    assert len(composer.get_completion_matches()) == 3
    composer.set_value("/mode")
    assert composer.edit("\t", []) == "changed"
    assert composer.value == "/mode "
    assert composer.edit("\x1b[B", []) == "changed"
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/mode confirm"
    assert composer.edit("\r", []) == "submit"

    composer.set_value("/skill ")
    assert [value for value, _ in composer.get_completion_matches()] == [
        "/skill git-helper",
        "/skill pytest-helper",
    ]

    composer.set_value("/resume ")
    assert any(
        value == f"/resume {first_session.session_id}"
        for value, _ in composer.get_completion_matches()
    )
    composer.set_value("/resu")
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/resume "
    assert composer.get_completion_matches()

    composer.set_value("/capabilities ")
    assert [value for value, _ in composer.get_completion_matches()] == [
        "/capabilities list",
        "/capabilities open",
        "/capabilities status",
        "/capabilities activate",
        "/capabilities release",
    ]

    composer.set_value("/capabilities open ")
    open_matches = [value for value, _ in composer.get_completion_matches()]
    assert "/capabilities open local:subagents" in open_matches
    assert app.engine.capability_warehouse.status()["opened"] == []

    composer.set_value("/capabilities activate --scope=run")
    unopened_capability_matches = [
        value for value, _ in composer.get_completion_matches()
    ]
    assert (
        "/capabilities activate --scope=run local:subagents"
        in unopened_capability_matches
    )
    assert app.engine.capability_warehouse.status()["opened"] == []

    composer.set_value("/capabilities o")
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/capabilities open "
    assert composer.get_completion_matches()

    app.engine.capability_warehouse.open_toolbox("local:subagents")
    composer.set_value("/capabilities activate sub")
    activate_matches = [value for value, _ in composer.get_completion_matches()]
    assert activate_matches == []

    composer.set_value("/capabilities activate --scope=turn")
    capability_matches = [value for value, _ in composer.get_completion_matches()]
    assert capability_matches == [
        "/capabilities activate --scope=turn skill:git-helper",
        "/capabilities activate --scope=turn skill:pytest-helper",
        "/capabilities activate --scope=turn local:subagents",
    ]
    assert all("--scope=" not in value.rsplit(" ", 1)[-1] for value in capability_matches)
    assert composer.edit("\r", []) == "changed"
    assert composer.value == "/capabilities activate --scope=turn skill:git-helper"

    composer.set_value("/capabilities activate local:subagents ")
    scope_matches = [value for value, _ in composer.get_completion_matches()]
    assert "/capabilities activate local:subagents --scope=session" in scope_matches
    composer.set_value(
        "/capabilities activate local:subagents --scope=turn"
    )
    assert composer.edit("\r", []) == "submit"

    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer = composer
    option_rows = presenter._completion_rows(composer.get_completion_matches())
    plain_option_rows = [_plain(row) for row in option_rows]
    assert any("Options (" in row for row in plain_option_rows)
    assert any("--scope=turn" in row for row in plain_option_rows)
    assert all("/capabilities activate" not in row for row in plain_option_rows)

@pytest.mark.parametrize('payload', ['\n', '\r', 'y', '中文\r\n第二行\n', 'e\u0301😀'])
def test_terminal_paste_is_an_edit_event_even_for_control_keys(payload):
    from saiworks.interaction.tui import PasteInput

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'\x1b[200~' + payload.encode() + b'\x1b[201~\r')
        presenter = TUIConsolePresenter(input=os.fdopen(os.dup(read_fd)), output=StringIO())
        try:
            key = presenter._read_key()
            assert isinstance(key, PasteInput)
            assert presenter._composer.edit(key, []) == 'changed'
            assert presenter._composer.value == payload.replace('\r\n', '\n').replace('\r', '\n')
            presenter._composer.edit('继续', [])
            assert presenter._composer.value.endswith('继续')
            assert presenter._composer.edit(presenter._read_key(), []) == 'submit'
        finally:
            presenter._input.close()
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize('key', ['\x1b[13;2u', '\x1b[27;2;13~', '\x1b\r'])
def test_modified_enter_inserts_newline(key):
    composer = ComposerState()
    composer.edit('中文', [])
    assert composer.edit(key, []) == 'changed'
    assert composer.value == '中文\n'


@pytest.mark.parametrize('answer', ['用户回复', ''])
def test_runtime_wait_reply_bypasses_interrupt_and_clears_receiver(monkeypatch, answer):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    monkeypatch.setattr(presenter, '_can_use_native_input', lambda: True)
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: pytest.fail('reply interrupted runtime'))
    results = []
    worker = threading.Thread(target=lambda: results.append(presenter.request_user_input('问题', [])))
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while presenter._user_input_waiter is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert presenter._user_input_waiter is not None
        presenter._composer.edit(answer, [])
        assert presenter._submit_runtime_prompt() is False
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == [answer or None]
        assert presenter._queued_prompt is None
        assert presenter.controller.drain().run_status == RunStatus.WORKING
        assert presenter._user_input_waiter is None
    finally:
        presenter._runtime_active = False
        worker.join(timeout=2)
    signals = []
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
    presenter._composer.set_value('下一轮')
    assert presenter._submit_runtime_prompt() is True
    assert presenter._queued_prompt == '下一轮'
    assert len(signals) == 1


def test_pasted_approval_keys_never_approve(monkeypatch):
    from saiworks.interaction.tui import PasteInput

    presenter = TUIConsolePresenter(output=StringIO())
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    presenter.controller.publish(TUIEvent(TUIEventKind.APPROVAL_REQUESTED, entity_id='approval'))
    state = presenter.controller.drain()
    monkeypatch.setattr(presenter, '_resolve_approval', lambda *args: pytest.fail('paste resolved approval'))
    for value in ['y', '\r', '\n', 'n']:
        presenter._handle_approval_key(PasteInput(value), state)
    presenter._composer.set_value('任意文本')
    assert presenter._submit_runtime_prompt(state) is False
    assert presenter._queued_prompt is None


def test_runtime_input_loop_edits_then_replies_without_stopping(monkeypatch):
    from saiworks.interaction.tui import PasteInput
    from contextlib import nullcontext

    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    monkeypatch.setattr(presenter, '_can_use_native_input', lambda: True)
    monkeypatch.setattr(presenter, '_raw_input', nullcontext)
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: pytest.fail('editing/reply interrupted'))
    answers = []
    worker = threading.Thread(target=lambda: answers.append(presenter.request_user_input('问题', [])))
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while presenter._user_input_waiter is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert presenter._user_input_waiter is not None
        completed = presenter._user_input_waiter.completed
        keys = iter([PasteInput('中文\n第二行'), '\x7f', '段', '\r'])

        def ready(timeout):
            if completed.is_set():
                presenter._runtime_stop.set()
                return False
            return True

        monkeypatch.setattr(presenter, '_input_ready', ready)
        monkeypatch.setattr(presenter, '_read_key', lambda **kwargs: next(keys))
        presenter._runtime_input_loop()
        worker.join(timeout=2)
        assert answers == ['中文\n第二段']
        assert presenter._queued_prompt is None
    finally:
        presenter._runtime_active = False
        worker.join(timeout=2)


def test_runtime_stop_cancels_reply_receiver(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    monkeypatch.setattr(presenter, '_can_use_native_input', lambda: True)
    answers = []
    worker = threading.Thread(target=lambda: answers.append(presenter.request_user_input('问题', [])))
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while presenter._user_input_waiter is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert presenter._user_input_waiter is not None
        presenter._runtime_active = False
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert answers == [None]
        assert presenter._user_input_waiter is None
    finally:
        presenter._runtime_active = False
        worker.join(timeout=2)


@contextmanager
def _terminal_input_bytes(payload):
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd)
    try:
        os.write(write_fd, payload)
        yield TUIConsolePresenter(input=source, output=StringIO())
    finally:
        source.close()
        os.close(write_fd)


@pytest.mark.parametrize(('encoded', 'legacy'), [
    (b'\x1b[27u', '\x1b'),
    (b'\x1b[99;5u', '\x03'),
    (b'\x1b[100;5u', '\x04'),
    (b'\x1b[97;5u', '\x01'),
    (b'\x1b[101;5u', '\x05'),
    (b'\x1b[13u', '\r'),
    (b'\x1b[13;2u', '\x1b\r'),
    (b'\x1b[13;3u', '\x1b\r'),
    (b'\x1b[9u', '\t'),
    (b'\x1b[127u', '\x7f'),
    (b'\x1b[57414u', '\r'),
    (b'\x1b[57414;2u', '\x1b\r'),
    (b'\x1b[57417u', '\x1b[D'),
    (b'\x1b[57418u', '\x1b[C'),
    (b'\x1b[57419u', '\x1b[A'),
    (b'\x1b[57420u', '\x1b[B'),
    (b'\x1b[57423u', '\x1b[H'),
    (b'\x1b[57424u', '\x1b[F'),
    (b'\x1b[99;69u', '\x03'),  # Caps Lock + Ctrl+C
    (b'\x1b[27;5;99~', '\x03'),
    (b'\x1b[27;2;13~', '\x1b\r'),
    (b'\x1b[27;3;13~', '\x1b\r'),
])
def test_terminal_keyboard_protocol_decodes_existing_operations(encoded, legacy):
    with _terminal_input_bytes(encoded) as presenter:
        assert presenter._read_key() == legacy


def test_enhanced_keyboard_edits_unicode_multiline_and_submits():
    payload = ('中文\x1b[13;2u第二行\x1b[97;5u前'
               '\x1b[101;5u尾\x1b[127u\x1b[100;5u\r').encode()
    with _terminal_input_bytes(payload) as presenter:
        assert presenter.prompt_input() == '前中文\n第二行'


@pytest.mark.parametrize(('encoded', 'error'), [
    (b'\x1b[99;5u', KeyboardInterrupt),
    (b'\x1b[100;5u', EOFError),
])
def test_enhanced_keyboard_cancels_or_exits_idle_prompt(encoded, error):
    with _terminal_input_bytes(encoded) as presenter:
        with pytest.raises(error):
            presenter.prompt_input()


@pytest.mark.parametrize('encoded', [b'\x1b[27u', b'\x1b[99;5u'])
def test_enhanced_keyboard_interrupts_running_task(monkeypatch, encoded):
    signals = []
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
    with _terminal_input_bytes(encoded) as presenter:
        presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
        presenter._runtime_input_loop()
        assert len(signals) == 1
        assert signals[0][1] == signal.SIGINT
        assert presenter.controller.drain().run_status == RunStatus.CANCELLING


@pytest.mark.parametrize('encoded', [b'\x1b[27u', b'\x1b[99;5u'])
def test_enhanced_cancel_keys_preserve_approval_path(monkeypatch, encoded):
    decisions = []
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: pytest.fail('approval sent SIGINT'))
    with _terminal_input_bytes(b'\x1b[200~y\r\x1b[201~' + encoded) as presenter:
        presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
        presenter.controller.publish(TUIEvent(TUIEventKind.APPROVAL_REQUESTED, entity_id='approval'))

        def resolve(approval_id, approved):
            decisions.append((approval_id, approved))
            presenter._runtime_stop.set()

        monkeypatch.setattr(presenter, '_resolve_approval', resolve)
        presenter._runtime_input_loop()
        assert decisions == [('approval', False)]
        assert presenter._composer.value == ''


def test_reply_handoff_preserves_immediate_typing_and_second_submit(monkeypatch):
    printed = threading.Event()
    release = threading.Event()
    answers, signals = [], []
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
    with _terminal_input_bytes('答案\r新\r'.encode()) as presenter:
        presenter._runtime_active = True
        presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
        presenter._composer.set_value('草稿')
        presenter._composer.cursor = 1

        def gated_print(value=''):
            if value == '问题':
                printed.set()
                assert release.wait(2)

        monkeypatch.setattr(presenter, '_print', gated_print)
        worker = threading.Thread(target=lambda: answers.append(presenter.request_user_input('问题', [])))
        worker.start()
        try:
            assert printed.wait(2)
            presenter._runtime_input_loop()
            assert presenter._user_input_waiter is None
            assert presenter._queued_prompt == '草新稿'
            assert presenter._composer.value == ''
            assert len(signals) == 1
            release.set()
            worker.join(timeout=2)
            assert not worker.is_alive()
            assert answers == ['答案']
            assert presenter._composer.value == ''
            assert presenter._queued_prompt == '草新稿'
        finally:
            release.set()
            presenter._runtime_active = False
            worker.join(timeout=2)


@pytest.mark.parametrize('error', [KeyboardInterrupt, OSError])
def test_interrupted_question_output_cleans_receiver_before_next_input(monkeypatch, error):
    signals = []
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
    with _terminal_input_bytes('新任务\r'.encode()) as presenter:
        presenter._runtime_active = True
        presenter._composer.set_value('草稿')
        presenter._composer.cursor = 1

        def fail_output(value=''):
            raise error

        monkeypatch.setattr(presenter, '_print', fail_output)
        with pytest.raises(error):
            presenter.request_user_input('问题', [])
        assert presenter._user_input_waiter is None
        assert presenter._composer.value == '草稿'
        assert presenter._composer.cursor == 1
        presenter._composer.set_value('')
        presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
        presenter._runtime_input_loop()
        assert presenter._queued_prompt == '新任务'
        assert len(signals) == 1


def test_old_reply_callback_cannot_clear_a_new_question(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    monkeypatch.setattr(presenter, '_can_use_native_input', lambda: True)
    first_printed, second_printed, release_first = (threading.Event() for _ in range(3))
    answers = []

    def gated_print(value=''):
        if value == 'first':
            first_printed.set()
            assert release_first.wait(2)
        elif value == 'second':
            second_printed.set()

    monkeypatch.setattr(presenter, '_print', gated_print)
    first = threading.Thread(target=lambda: answers.append(presenter.request_user_input('first', [])))
    second = threading.Thread(target=lambda: answers.append(presenter.request_user_input('second', [])))
    first.start()
    try:
        assert first_printed.wait(2)
        presenter._composer.set_value('first answer')
        assert presenter._submit_runtime_prompt() is False
        second.start()
        assert second_printed.wait(2)
        receiver = presenter._user_input_waiter
        presenter._composer.set_value('second answer')
        release_first.set()
        first.join(timeout=2)
        assert not first.is_alive()
        assert presenter._user_input_waiter is receiver
        assert presenter._composer.value == 'second answer'
        assert presenter._submit_runtime_prompt() is False
        second.join(timeout=2)
        assert not second.is_alive()
        assert answers == ['first answer', 'second answer']
    finally:
        release_first.set()
        presenter._runtime_active = False
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)


def test_runtime_stop_restores_draft_before_saving_next_prompt(monkeypatch):
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._runtime_active = True
    presenter._composer.set_value('next draft')
    printed, release = threading.Event(), threading.Event()
    answers = []
    monkeypatch.setattr(presenter, '_can_use_native_input', lambda: True)

    def gated_print(value=''):
        if value == 'question':
            printed.set()
            assert release.wait(2)

    monkeypatch.setattr(presenter, '_print', gated_print)
    worker = threading.Thread(target=lambda: answers.append(presenter.request_user_input('question', [])))
    worker.start()
    try:
        assert printed.wait(2)
        presenter._composer.set_value('unsubmitted partial answer')
        presenter._stop_runtime(TUIEventKind.RUN_FAILED)
        assert presenter._user_input_waiter is None
        assert presenter._composer_draft == 'next draft'
        presenter._composer.edit('!', [])
        release.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert answers == [None]
        assert presenter._composer.value == 'next draft!'
    finally:
        release.set()
        presenter._runtime_active = False
        worker.join(timeout=2)


@pytest.mark.parametrize('payload', ['\x03', '\x1b', '\n', '中文\r\n多行'])
def test_idle_paste_never_dispatches_shortcut_or_submit(payload):
    data = b'\x1b[200~' + payload.encode() + b'\x1b[201~' + '继续\r'.encode()
    with _terminal_input_bytes(data) as presenter:
        assert presenter.prompt_input() == (payload.replace('\r\n', '\n') + '继续').strip()


@pytest.mark.parametrize('paste', [False, True])
def test_submit_consumes_composer_and_only_history_can_restore_it(paste):
    text = '提交任务' if not paste else '中文多行任务\n' + '长文本内容' * 40 + '\n最后一行'
    encoded = text.encode()
    if paste:
        encoded = b'\x1b[200~' + encoded + b'\x1b[201~'
    with _terminal_input_bytes(encoded + b'\r') as presenter:
        assert presenter.prompt_input() == text
        assert presenter._composer.value == ''
        assert presenter._composer.cursor == 0
        assert presenter._composer.history_index is None
        assert presenter._composer_draft == ''
        assert presenter._history == [text]
        presenter._composer.edit('\x1b[A', presenter._history)
        assert presenter._composer.value == text


def test_queued_submission_consumes_composer_before_interrupt(monkeypatch):
    with _terminal_input_bytes(b'') as presenter:
        presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
        presenter._composer.set_value('明确提交的新消息\n第二行')
        signals = []
        monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
        assert presenter._submit_runtime_prompt() is True
        assert presenter._queued_prompt == '明确提交的新消息\n第二行'
        assert presenter._composer.value == ''
        assert len(signals) == 1
        assert presenter.prompt_input() == '明确提交的新消息\n第二行'
        assert presenter._queued_prompt is None
        assert presenter._composer.value == ''


@pytest.mark.parametrize('kind', [TUIEventKind.APPROVAL_SELECTED, TUIEventKind.APPROVAL_RESOLVED])
def test_stale_approval_events_cannot_change_current_approval(kind):
    controller = TUIController()
    controller.publish(TUIEvent(TUIEventKind.APPROVAL_REQUESTED, entity_id='current'))
    state = controller.drain()
    controller.publish(TUIEvent(kind, entity_id='old', payload={'selected': 1}))
    assert controller.drain() == state


def test_unknown_approval_decision_does_not_clear_current_approval_or_touch_draft():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value('尚未提交的草稿\n第二行')
    presenter._composer.cursor = 2
    presenter._history = ['已经提交的历史消息']
    completed, results = threading.Event(), []
    presenter._approval_waiters['current'] = completed, results
    presenter.controller.publish(TUIEvent(TUIEventKind.APPROVAL_REQUESTED, entity_id='current'))
    state = presenter.controller.drain()
    presenter._resolve_approval('old', True)
    assert presenter.controller.drain() == state
    assert not completed.is_set()
    presenter._handle_approval_key('\r', state)
    assert completed.is_set()
    assert results == [True]
    assert presenter.controller.drain().approval is None
    assert presenter._composer.value == '尚未提交的草稿\n第二行'
    assert presenter._composer.cursor == 2
    assert presenter._history == ['已经提交的历史消息']
    assert presenter._queued_prompt is None


def test_resume_retires_only_abandoned_progress_views_and_keeps_draft_and_elapsed():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED, created_at=42.0))
    completed = presenter.tool_started('read_file')
    presenter.tool_finished(completed, SimpleNamespace(name='read_file'),
                            SimpleNamespace(success=True, output='read', error_code=None, name='read_file'))
    abandoned = presenter.tool_started('request_user_input')
    presenter._composer.set_value('未提交草稿\n继续编辑')
    presenter._composer.cursor = 2
    presenter._history = ['original request']
    # Leave start/finish events queued, as when the renderer has not refreshed
    # before the next execution segment begins.
    presenter.show_run_resumed()
    resumed = presenter.tool_started('request_user_input')
    presenter.tool_finished(resumed, SimpleNamespace(name='request_user_input'),
                            SimpleNamespace(success=True, output='reply', error_code=None, name='request_user_input'))
    state = presenter.controller.drain()
    assert {tool.tool_id for tool in state.tools} == {completed, resumed}
    assert abandoned not in presenter._active_tool_names
    assert not presenter._active_tool_names
    assert all(tool.status == ToolStatus.SUCCEEDED for tool in state.tools)
    assert state.run_started_at == 42.0
    assert state.run_status == RunStatus.WORKING
    assert presenter._composer.value == '未提交草稿\n继续编辑'
    assert presenter._composer.cursor == 2
    assert presenter._history == ['original request']
    assert presenter._queued_prompt is None
    presenter._flush_tool_transcript()
    assert presenter._output.getvalue().count('read_file →') == 1
    assert presenter._output.getvalue().count('request_user_input →') == 1
    assert 'Executing' not in presenter._output.getvalue()
    assert 'aborted' not in presenter._output.getvalue()


@pytest.mark.parametrize('terminal', [False, True])
def test_partial_paste_survives_runtime_stop_and_continues_in_idle_prompt(monkeypatch, terminal):
    if terminal:
        write_fd, read_fd = os.openpty()
    else:
        read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    presenter._runtime_active = True
    presenter._composer.set_value('草稿:')
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    entered = threading.Event()
    read_paste = presenter._read_paste

    def observe_paste(fd, **kwargs):
        entered.set()
        return read_paste(fd, **kwargs)

    monkeypatch.setattr(presenter, '_read_paste', observe_paste)
    reader = threading.Thread(target=presenter._runtime_input_loop, daemon=True)
    presenter._runtime_input_thread = reader
    chinese = '中'.encode()
    try:
        os.write(write_fd, b'\x1b[200~' + '第一行\n'.encode() + chinese[:1])
        reader.start()
        assert entered.wait(2)
        presenter._stop_runtime(TUIEventKind.RUN_FINISHED)
        assert not reader.is_alive()
        assert presenter._runtime_input_thread is None
        os.write(write_fd, chinese[1:] + b'\x1b[201~' + '继续\r'.encode())
        assert presenter.prompt_input() == '草稿:第一行\n中继续'
        assert presenter._composer.value == ''
    finally:
        presenter._runtime_stop.set()
        os.write(write_fd, b'\x1b[201~')
        if reader.ident is not None:
            reader.join(timeout=2)
        source.close()
        os.close(write_fd)


@pytest.mark.parametrize(('payload', 'text'), [
    ('中'.encode(), '中'),
    (b'\x1b[200~y\n\x1b[201~', 'y\n'),
])
def test_decoded_event_is_handed_to_idle_when_runtime_stops_before_dispatch(monkeypatch, payload, text):
    from saiworks.interaction.tui import PasteInput

    decoded, release = threading.Event(), threading.Event()
    with _terminal_input_bytes(payload) as presenter:
        presenter._runtime_active = True
        presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
        read_key = presenter._read_key

        def hold_key(**kwargs):
            key = read_key(**kwargs)
            if key is not None and not decoded.is_set():
                decoded.set()
                assert release.wait(2)
            return key

        monkeypatch.setattr(presenter, '_read_key', hold_key)
        reader = threading.Thread(target=presenter._runtime_input_loop, daemon=True)
        presenter._runtime_input_thread = reader
        stopper = threading.Thread(target=lambda: presenter._stop_runtime(TUIEventKind.RUN_FINISHED), daemon=True)
        reader.start()
        try:
            assert decoded.wait(2)
            stopper.start()
            assert presenter._runtime_stop.wait(2)
            release.set()
            stopper.join(timeout=2)
            assert not stopper.is_alive()
            assert not reader.is_alive()
            assert presenter._input_ready(timeout=0)
            key = presenter._read_key()
            if text == 'y\n':
                assert isinstance(key, PasteInput)
            assert presenter._composer.edit(key, []) == 'changed'
            assert presenter._composer.value == text
        finally:
            release.set()
            presenter._runtime_stop.set()
            reader.join(timeout=2)
            if stopper.ident is not None:
                stopper.join(timeout=2)


@pytest.mark.parametrize(('payload', 'visible'), [
    ('\x1b[2J', '\\x1b[2J'),
    ('\x1b]52;c;dGVzdA==\x07', '\\x1b]52;c;dGVzdA==\\x07'),
    ('\x9b2J', '\\x9b2J'),
    ('\t\x00\x03\x7f', '\\t\\x00\\x03\\x7f'),
])
def test_input_controls_are_visible_text_in_composer_and_submitted_transcript(payload, visible):
    from saiworks.interaction.tui import PasteInput

    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.edit(PasteInput(payload), [])
    rows, _, _ = presenter._composer_rows(200)
    assert visible in '\n'.join(rows)
    assert payload not in '\n'.join(rows)
    assert presenter._composer.value == payload
    presenter.show_start(SimpleNamespace(prompt=payload, cwd='/workspace'))
    presenter.show_user_prompt(payload)
    output = presenter._output.getvalue()
    assert output.count(visible) == 2
    assert payload not in output


def test_escaped_input_wraps_using_display_width_and_original_character_cursor():
    presenter = TUIConsolePresenter(output=StringIO())
    presenter._composer.set_value('中\x1b[2J文')
    lines, row, column, _ = presenter._wrap_composer(20)
    assert lines == ['中\\x1b[2', 'J文']
    assert (row, column) == (1, 3)
    presenter._composer.cursor = 2
    _, row, column, _ = presenter._wrap_composer(20)
    assert (row, column) == (0, 6)


@pytest.mark.parametrize(('first', 'rest', 'expected'), [
    ('中'.encode()[:1], '中'.encode()[1:], '中'),
    (b'\x1b[20', b'0~' + '中文\n第二行'.encode() + b'\x1b[201~', '中文\n第二行'),
    (b'\x1b[200~' + '中文'.encode() + b'\x1b[20', b'1~', '中文'),
])
def test_incremental_decoder_retains_split_unicode_and_paste_markers(first, rest, expected):
    from saiworks.interaction.tui import PasteInput

    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    try:
        os.write(write_fd, first)
        assert presenter._read_key() is None
        os.write(write_fd, rest + b'\r')
        key = presenter._read_key()
        assert key == expected
        if first.startswith(b'\x1b'):
            assert isinstance(key, PasteInput)
        assert presenter._read_key() == '\r'
    finally:
        source.close()
        os.close(write_fd)


def test_terminal_mode_entry_preserves_already_queued_input():
    master_fd, slave_fd = os.openpty()
    source = os.fdopen(slave_fd)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    try:
        os.write(master_fd, '预先输入\n'.encode())
        with presenter._raw_input():
            assert presenter._input_ready(timeout=0.2)
            text = ''.join(presenter._read_key() for _ in range(5))
            assert text == '预先输入\n'
    finally:
        source.close()
        os.close(master_fd)


@pytest.mark.parametrize('partial', [b'\xe4', b'\x1b[200~\xe4'])
def test_eof_finishes_partial_decoder_state_without_waiting_forever(partial):
    from saiworks.interaction.tui import PasteInput

    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    try:
        os.write(write_fd, partial)
        os.close(write_fd)
        key = presenter._read_key()
        assert key == '\ufffd'
        assert isinstance(key, PasteInput) == partial.startswith(b'\x1b')
        with pytest.raises(EOFError):
            presenter._read_key()
    finally:
        source.close()


def test_runtime_eof_uses_existing_interrupt_path(monkeypatch):
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    presenter._runtime_active = True
    presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
    signals = []
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
    os.close(write_fd)
    try:
        presenter._runtime_input_loop()
        assert presenter.controller.drain().run_status == RunStatus.CANCELLING
        assert len(signals) == 1
        assert signals[0][1] == signal.SIGINT
    finally:
        source.close()


def test_real_terminal_paste_preserves_signal_and_flow_control_bytes():
    import termios
    from saiworks.interaction.tui import PasteInput

    master, slave = os.openpty()
    source = os.fdopen(slave)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    previous = termios.tcgetattr(slave)
    payload = '开头'.encode() + b'\x03\x13\x11\r\n' + '结尾'.encode()
    try:
        with presenter._raw_input():
            active = termios.tcgetattr(slave)
            assert not active[3] & termios.ISIG
            assert not active[0] & (termios.IXON | termios.ICRNL)
            assert active[1] == previous[1]
            os.write(master, b'\x1b[200~' + payload + b'\x1b[201~')
            assert presenter._input_ready(timeout=0.2)
            key = presenter._read_key()
            assert isinstance(key, PasteInput)
            assert key.encode() == payload
            assert presenter._composer.edit(key, []) == 'changed'
            assert presenter._composer.value == '开头\x03\x13\x11\n结尾'
            os.write(master, b'\x03')
            assert presenter._input_ready(timeout=0.2)
            assert presenter._read_key() == '\x03'
        assert termios.tcgetattr(slave) == previous
    finally:
        source.close()
        os.close(master)


@pytest.mark.parametrize(('choice', 'approved'), [(b'1\r', True), (b'2\r', False)])
def test_compaction_selection_consumes_shared_typeahead_without_new_user_input(choice, approved):
    from saiworks.interaction.cli import CLI

    with _terminal_input_bytes('任务\r'.encode() + choice) as presenter:
        cli = CLI(None, presenter, runtime=SimpleNamespace(view=lambda: None))
        assert presenter.prompt_input() == '任务'
        assert cli._confirm_compaction('confirm') == approved
        assert presenter.prompt_input() == ''
        assert presenter._composer.value == ''
        assert presenter._history == ['任务']
        assert presenter._queued_prompt is None


@pytest.mark.parametrize('pasted', [b'y', b'1', b'\r', b'\x03'])
def test_shared_selection_ignores_paste_even_when_payload_is_a_shortcut(pasted):
    data = '任务\r'.encode() + b'\x1b[200~' + pasted + b'\x1b[201~n'
    with _terminal_input_bytes(data) as presenter:
        assert presenter.prompt_input() == '任务'
        assert presenter.prompt_box.read_selection() == '2'
        assert presenter._history == ['任务']
        assert presenter._composer.value == ''


def test_session_picker_consumes_shared_prefetched_arrow_and_enter(monkeypatch):
    from saiworks.interaction.cli import CLI

    sessions = [SimpleNamespace(session_id=identity, status='active', updated_at='now', preview=identity, message_count=1, cwd='/workspace')
                for identity in ['first', 'second']]
    with _terminal_input_bytes('任务\r'.encode() + b'\x1b[B\r') as presenter:
        cli = CLI(None, presenter, runtime=SimpleNamespace(view=lambda: None))
        monkeypatch.setattr(cli, 'list_sessions', lambda: sessions)
        monkeypatch.setattr(cli, 'load_session', lambda identity: next(s for s in sessions if s.session_id == identity))
        assert presenter.prompt_input() == '任务'
        assert cli.choose_session() is sessions[1]
        assert presenter._history == ['任务']
        assert presenter._composer.value == ''
        assert presenter._queued_prompt is None


@pytest.mark.parametrize('running', [False, True])
def test_real_terminal_ctrl_c_still_cancels_outside_paste(monkeypatch, running):
    import termios

    master, slave = os.openpty()
    source = os.fdopen(slave)
    presenter = TUIConsolePresenter(input=source, output=StringIO())
    previous = termios.tcgetattr(slave)
    original_mode = presenter._raw_input
    signals = []

    @contextmanager
    def mode_with_ctrl_c():
        with original_mode():
            assert not termios.tcgetattr(slave)[3] & termios.ISIG
            os.write(master, b'\x03')
            yield

    monkeypatch.setattr(presenter, '_raw_input', mode_with_ctrl_c)
    monkeypatch.setattr('saiworks.interaction.tui.os.kill', lambda *args: signals.append(args))
    try:
        if running:
            presenter._runtime_active = True
            presenter.controller.publish(TUIEvent(TUIEventKind.RUN_STARTED))
            presenter._runtime_input_loop()
            assert len(signals) == 1
            assert signals[0][1] == signal.SIGINT
            assert presenter.controller.drain().run_status == RunStatus.CANCELLING
        else:
            with pytest.raises(KeyboardInterrupt):
                presenter.prompt_input()
            assert signals == []
        assert termios.tcgetattr(slave) == previous
    finally:
        source.close()
        os.close(master)


@pytest.mark.parametrize('key', [b'\x1b[27u', b'\x1b[99;5u'])
def test_shared_selection_cancel_keys_do_not_edit_composer(key):
    with _terminal_input_bytes('任务\r'.encode() + key) as presenter:
        assert presenter.prompt_input() == '任务'
        presenter._composer.set_value('保留草稿')
        assert presenter.prompt_box.read_selection() is None
        assert presenter._composer.value == '保留草稿'
        assert presenter._history == ['任务']


def test_running_selection_cannot_open_another_reader():
    with _terminal_input_bytes(b'1') as presenter:
        presenter._runtime_active = True
        with pytest.raises(RuntimeError, match='runtime input dispatcher'):
            presenter.prompt_box.read_selection()
        presenter._runtime_active = False
        assert presenter._read_key() == '1'


@pytest.mark.parametrize('when', ['enter', 'exit'])
def test_terminal_settings_and_reader_ownership_restore_on_output_failure(when):
    import termios

    class Output(StringIO):
        fail = False

        def write(self, value):
            if self.fail:
                raise OSError('closed output')
            return super().write(value)

    master, slave = os.openpty()
    source = os.fdopen(slave)
    output = Output()
    presenter = TUIConsolePresenter(input=source, output=output)
    previous = termios.tcgetattr(slave)
    released = threading.Event()
    try:
        output.fail = when == 'enter'
        with pytest.raises(OSError, match='closed output'):
            with presenter._raw_input():
                output.fail = True
        assert termios.tcgetattr(slave) == previous

        def acquire_reader():
            if presenter._reader_lock.acquire(timeout=0.2):
                presenter._reader_lock.release()
                released.set()

        checker = threading.Thread(target=acquire_reader)
        checker.start()
        checker.join(timeout=1)
        assert released.is_set()
    finally:
        source.close()
        os.close(master)
