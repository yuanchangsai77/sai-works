from dataclasses import FrozenInstanceError
from queue import Queue
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from saiworks.interaction.desktop import DesktopController
from saiworks.interaction.desktop_app import DesktopApp
from saiworks.interaction.desktop_presenter import (
    DesktopActivityModel,
    DesktopProgressReporter,
    format_approval_prompt,
)
from saiworks.interaction.desktop_windows import (
    ApprovalDialog,
    QuickAction,
    elapsed_time_label,
    filter_quick_actions,
    open_run_logs,
    show_approval_dialog,
)
from saiworks.runtime import Runtime
from saiworks.sessions import SessionStore
from saiworks.sessions.tasks import TaskStore
from saiworks.types import ExecutionSummary, SessionRunTrace, TaskCheckpoint, UserRequest


def test_desktop_controller_runs_and_persists_without_terminal(tmp_path):
    class Engine:
        def execute(self, request: UserRequest):
            return ExecutionSummary(f"done:{request.prompt}", [])

    sessions = SessionStore(base_dir=tmp_path / "sessions")
    runtime = Runtime(
        Engine(),
        session_store=sessions,
        task_store=TaskStore(tmp_path / "tasks.sqlite3"),
    )
    controller = DesktopController(runtime)

    initial = controller.open_workspace(tmp_path)
    summary = controller.submit("inspect workspace", context_paths=["README.md"])
    current = controller.view()

    assert initial.messages == ()
    assert summary.final_message == "done:inspect workspace"
    assert current.messages[-1] == ("assistant", "done:inspect workspace")
    assert sessions.load(current.session_id).messages[-1]["content"] == summary.final_message
    with pytest.raises(FrozenInstanceError):
        current.status = "changed"


def test_desktop_controller_resumes_session_and_forwards_cancel(tmp_path):
    class Engine:
        cancelled = 0

        def execute(self, _request):
            return ExecutionSummary("done", [])

        def cancel_current_run(self):
            self.cancelled += 1

    sessions = SessionStore(base_dir=tmp_path / "sessions")
    stored = sessions.create(str(tmp_path), messages=[{"role": "assistant", "content": "earlier"}])
    engine = Engine()
    controller = DesktopController(Runtime(engine, session_store=sessions))

    view = controller.resume_latest()
    tasks = controller.list_tasks(cwd=tmp_path)
    diagnostics = controller.diagnostics()
    controller.cancel_current()
    controller.close()

    assert view.messages == (("assistant", "earlier"),)
    assert tasks[0].session_id == stored.session_id
    assert tasks[0].title == "Untitled task"
    assert diagnostics.session_id == stored.session_id
    assert engine.cancelled == 1
    with pytest.raises(RuntimeError, match="Open a workspace"):
        controller.view()
    with pytest.raises(RuntimeError, match="Runtime is closed"):
        controller.open_workspace(tmp_path)


def test_switching_workspace_keeps_runtime_open_until_controller_closes(tmp_path):
    class Engine:
        def execute(self, _request):
            return ExecutionSummary("done", [])

    runtime = Runtime(Engine(), session_store=SessionStore(base_dir=tmp_path / "sessions"))
    controller = DesktopController(runtime)

    controller.open_workspace(tmp_path / "first")
    controller.open_workspace(tmp_path / "second")
    assert runtime.is_running is False

    controller.close()
    controller.close()
    with pytest.raises(RuntimeError, match="Runtime is closed"):
        runtime.execute(UserRequest("after close", str(tmp_path)))


def test_runtime_close_requires_idle_or_explicit_force(tmp_path):
    started = Event()
    released = Event()

    class Tools:
        reset_count = 0

        def reset_state(self):
            self.reset_count += 1

    class Engine:
        def __init__(self):
            self.tools = Tools()
            self.cancelled = 0

        def execute(self, _request):
            started.set()
            assert released.wait(2)
            return ExecutionSummary("stopped", [], outcome="interrupted")

        def cancel_current_run(self):
            self.cancelled += 1
            released.set()

    engine = Engine()
    runtime = Runtime(engine)
    worker = Thread(target=runtime.execute, args=(UserRequest("wait", str(tmp_path)),))
    worker.start()
    assert started.wait(2)

    with pytest.raises(RuntimeError, match="still running"):
        runtime.close()
    runtime.close(force=True)
    worker.join(2)

    assert not worker.is_alive()
    assert runtime.is_closed
    assert engine.cancelled == 1
    assert engine.tools.reset_count == 1


def test_desktop_progress_reporter_exposes_only_presentation_text():
    messages = []
    reporter = DesktopProgressReporter(messages.append)
    action = type("Action", (), {"name": "read_file"})()
    result = type("Result", (), {"success": True})()

    handle = reporter.model_started()
    reporter.model_retrying(handle, 1, 3, "temporarily unavailable", 0.5)
    reporter.tool_started(action.name)
    reporter.tool_finished(action.name, action, result)

    assert messages == [
        "Model is thinking…",
        "Model retry 1/3: temporarily unavailable; waiting 0.5s",
        "Read a workspace file…",
        "Read a workspace file completed",
    ]


def test_desktop_task_title_hides_stable_request_envelope():
    assert DesktopController._task_title(
        "Current working directory: /repo\nUser request: improve the desktop"
    ) == "improve the desktop"


def test_desktop_activity_model_preserves_execution_semantics():
    activity = DesktopActivityModel()

    activity.start()
    activity.update("Read a workspace file…")
    label = activity.finish("completed")

    assert [step.state for step in activity.steps] == ["done", "done", "done"]
    assert activity.steps[1].label == "Read a workspace file"
    assert label == "Completed and verified"
    assert activity.headline == "✓  Completed and verified"


def test_desktop_activity_model_marks_incomplete_outcome_as_failed():
    activity = DesktopActivityModel()
    activity.start()

    activity.finish("runtime_error")

    assert activity.steps[-2].state == "failed"
    assert activity.steps[-1].state == "failed"
    assert activity.headline == "!  Runtime error"


@pytest.mark.parametrize(
    ("outcome", "state", "headline"),
    [
        ("interrupted", "cancelled", "■  Cancelled and stopped"),
        ("blocked", "blocked", "!  Blocked: needs your input"),
        ("exhausted", "warning", "!  Execution limit reached"),
        ("stalled", "warning", "!  Stopped making progress"),
    ],
)
def test_desktop_activity_model_preserves_non_failure_terminal_states(outcome, state, headline):
    activity = DesktopActivityModel()
    activity.start()

    activity.finish(outcome)

    assert activity.steps[-1].state == state
    assert activity.headline == headline


def test_desktop_approval_identifies_saiworks_and_explains_macos_host():
    action = type("Action", (), {"name": "shell_exec", "arguments": {"command": "open -a Music"}})()

    prompt = format_approval_prompt(action, "execution requires explicit approval")

    assert "SaiWorks Runtime is requesting permission" in prompt
    assert "Action: Run a local command" in prompt
    assert '"command": "open -a Music"' in prompt
    assert "Zed, Terminal, or Python" in prompt


def test_desktop_quick_actions_filter_without_losing_callbacks():
    selected = []
    actions = (
        QuickAction("New task", lambda: selected.append("new")),
        QuickAction("Open diagnostics", lambda: selected.append("diagnostics")),
    )

    matches = filter_quick_actions(actions, "  DIAG  ")
    matches[0].callback()

    assert [action.label for action in matches] == ["Open diagnostics"]
    assert selected == ["diagnostics"]


def test_desktop_open_run_logs_reports_missing_and_opener_errors(tmp_path):
    messages = []

    class MessageBox:
        def showinfo(self, title, message):
            messages.append(("info", title, message))

        def showerror(self, title, message):
            messages.append(("error", title, message))

    assert open_run_logs("", MessageBox(), opener=lambda _command: None) is False
    assert open_run_logs(str(tmp_path / "missing"), MessageBox(), opener=lambda _command: None) is False

    logs = tmp_path / "logs"
    logs.mkdir()

    def fail(_command):
        raise OSError("Finder unavailable")

    assert open_run_logs(str(logs), MessageBox(), opener=fail) is False
    assert [item[1] for item in messages] == ["No logs yet", "Logs not found", "Unable to open logs"]


def test_desktop_diagnostics_projects_runtime_trace_facts(tmp_path):
    sessions = SessionStore(base_dir=tmp_path / "sessions")
    stored = sessions.create(str(tmp_path))
    stored.trace.append(SessionRunTrace(
        run_id="run-1",
        started_at="2026-10-02T00:00:00+00:00",
        completed_at="2026-10-02T00:01:05+00:00",
        prompt="inspect",
        final_message="done",
        outcome="completed",
        event_count=12,
        turn_count=3,
        checkpoint=TaskCheckpoint(phase="verified"),
    ))
    sessions.save(stored)
    controller = DesktopController(Runtime(object(), session_store=sessions))

    controller.resume(stored.session_id)
    diagnostic = controller.diagnostics()

    assert diagnostic.event_count == 12
    assert diagnostic.turn_count == 3
    # Session preparation normalizes a resumable checkpoint back to in-progress;
    # Desktop must show that Runtime fact instead of preserving a stale label.
    assert diagnostic.verification_phase == "in_progress"
    assert elapsed_time_label(diagnostic.started_at, diagnostic.completed_at) == "1m 05s"


def test_desktop_event_drain_does_not_reschedule_after_close():
    class Root:
        scheduled = 0

        def after(self, _delay, _callback):
            self.scheduled += 1
            return "timer"

    app = DesktopApp.__new__(DesktopApp)
    app.root = Root()
    app._events = Queue()
    app._events.put(("finished", ExecutionSummary("done", [])))
    app._closed = False
    app._event_timer = "active-timer"
    app._append_result = lambda _summary: None
    app._set_running = lambda _running: None
    app._finish_activity = lambda _outcome: None
    app._refresh_tasks = lambda: None

    def close_if_requested():
        app._closed = True

    app._close_if_requested = close_if_requested

    app._drain_events()

    assert app._event_timer is None
    assert app.root.scheduled == 0


def test_desktop_close_rejects_pending_approval_and_releases_worker():
    action = type("Action", (), {"name": "shell_exec", "arguments": {}})()
    decisions = []

    class Controller:
        cancelled = 0

        def cancel_current(self):
            self.cancelled += 1

    class Status:
        def set(self, _message):
            return None

    app = DesktopApp.__new__(DesktopApp)
    app._closed = False
    app._close_requested = False
    app._running = True
    app._approval_lock = Lock()
    app._pending_approvals = []
    app._approval_dialogs = {}
    app._events = Queue()
    app.controller = Controller()
    app.status = Status()
    app._schedule_shutdown_check = lambda: None

    worker = Thread(
        target=lambda: decisions.append(app._request_approval(action, "approval required"))
    )
    worker.start()
    kind, request = app._events.get(timeout=1)

    assert kind == "approval"
    assert request.is_resolved is False

    class Dialog:
        closed = False

        def close(self, approved):
            self.closed = True
            request.resolve(approved)

    dialog = Dialog()
    app._approval_dialogs[request] = dialog

    app.request_close(confirm=False)
    worker.join(1)

    assert decisions == [False]
    assert dialog.closed is True
    assert app.controller.cancelled == 1
    assert app._close_requested is True


def test_desktop_approval_dialog_close_is_idempotent():
    class Window:
        releases = 0
        destroys = 0

        def grab_release(self):
            self.releases += 1

        def destroy(self):
            self.destroys += 1

    decisions = []
    window = Window()
    dialog = ApprovalDialog(window, decisions.append)

    dialog.close(False)
    dialog.close(True)

    assert decisions == [False]
    assert window.releases == 1
    assert window.destroys == 1


@pytest.mark.parametrize("approved", [False, True])
def test_approval_waits_for_visible_window_and_ignores_late_map(approved):
    class Widget:
        def __init__(self, *_args, **_kwargs):
            self.bindings = {}
            self.visible = False
            self.grabs = 0
            self.focuses = 0

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

        def bind(self, event, callback):
            self.bindings[event] = callback

        def winfo_viewable(self):
            return self.visible

        def grab_set(self):
            assert self.visible, "grab failed: window not viewable"
            self.grabs += 1

        def focus_set(self):
            self.focuses += 1

    toolkit = SimpleNamespace(Toplevel=Widget, Text=Widget, Label=Widget, Frame=Widget, Button=Widget)
    decisions = []
    dialog = show_approval_dialog(None, toolkit, toolkit, "Review action", decisions.append)
    window = dialog.window
    on_map = window.bindings["<Map>"]
    assert (window.grabs, window.focuses) == (0, 0)

    on_map(SimpleNamespace(widget=window))
    window.visible = True
    on_map(SimpleNamespace(widget=Widget()))
    assert (window.grabs, window.focuses) == (0, 0)

    on_map(SimpleNamespace(widget=window))
    assert (window.grabs, window.focuses) == (1, 1)
    dialog.close(approved)
    on_map(SimpleNamespace(widget=window))
    assert (window.grabs, window.focuses) == (1, 1)
    assert decisions == [approved]


def test_approval_can_close_before_window_is_visible():
    class Window:
        def grab_release(self):
            raise RuntimeError("No grab yet")

        def destroy(self):
            pass

    decisions = []
    dialog = ApprovalDialog(Window(), decisions.append)
    dialog.close(False)
    assert decisions == [False]


def test_desktop_elapsed_time_handles_incompatible_timezones():
    assert elapsed_time_label(
        "2026-10-02T00:00:00",
        "2026-10-02T00:01:00+00:00",
    ) == "Unavailable"
