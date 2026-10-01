from dataclasses import FrozenInstanceError
from threading import Event, Thread

import pytest

from saiworks.interaction.desktop import DesktopController
from saiworks.interaction.desktop_app import DesktopProgressReporter, format_approval_prompt
from saiworks.runtime import Runtime
from saiworks.sessions import SessionStore
from saiworks.sessions.tasks import TaskStore
from saiworks.types import ExecutionSummary, UserRequest


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
    controller.cancel_current()
    controller.close()

    assert view.messages == (("assistant", "earlier"),)
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
        "Running read_file…",
        "read_file completed",
    ]


def test_desktop_approval_identifies_saiworks_and_explains_macos_host():
    action = type("Action", (), {"name": "shell_exec", "arguments": {"command": "open -a Music"}})()

    prompt = format_approval_prompt(action, "execution requires explicit approval")

    assert "SaiWorks Runtime is requesting permission" in prompt
    assert '"command": "open -a Music"' in prompt
    assert "Zed, Terminal, or Python" in prompt
