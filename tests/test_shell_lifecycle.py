from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, RLock
import subprocess

import pytest

from saiworks.observability.logger import InMemoryLogger
from saiworks.tools.builtins.shell_exec import ShellSession, _shell_session, tool
from saiworks.tools.registry import ToolRegistry
from saiworks.types import ToolAction


def registry(tmp_path):
    result = ToolRegistry(InMemoryLogger(base_dir=str(tmp_path / 'runs')))
    result.register(tool())
    return result


def test_cancel_during_command_read_never_resurrects_shell(tmp_path, monkeypatch):
    tools = registry(tmp_path)
    started, release = Event(), Event()
    original = ShellSession._read_until_marker
    processes = []
    start = ShellSession._start

    def track_start(self, cwd):
        process = start(self, cwd)
        processes.append(process)
        return process

    def paused_read(self, marker, timeout):
        started.set()
        assert release.wait(3)
        return original(self, marker, timeout)

    monkeypatch.setattr(ShellSession, '_start', track_start)
    monkeypatch.setattr(ShellSession, '_read_until_marker', paused_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(tools.execute, ToolAction('shell_exec', {'command': 'sleep 30'}), cwd=str(tmp_path))
        assert started.wait(3)
        session = tools.state_for('shell_session')
        try:
            tools.reset_state()
        finally:
            release.set()
        result = future.result(timeout=3)
    assert not result.success
    assert session.closed
    assert len(processes) == 1
    assert all(process.poll() is not None for process in processes)
    assert tools.state_for('shell_session') is None


def test_stale_context_cannot_create_shell_after_reset(tmp_path):
    tools = registry(tmp_path)
    context = tools._context(str(tmp_path), None)
    tools.reset_state()
    with pytest.raises(KeyboardInterrupt):
        _shell_session(context, tmp_path)
    assert tools.state_for('shell_session') is None


def test_timeout_closes_instance_and_next_call_lazily_creates_new_one(tmp_path):
    tools = registry(tmp_path)
    try:
        failed = tools.execute(ToolAction('shell_exec', {'command': 'sleep 30', 'timeout': 0.02}), cwd=str(tmp_path))
        previous = tools.state_for('shell_session')
        assert failed.error_code == 'timeout'
        assert previous.closed
        assert previous.process.poll() is not None
        result = tools.execute(ToolAction('shell_exec', {'command': 'printf ready'}), cwd=str(tmp_path))
        assert result.success
        assert tools.state_for('shell_session') is not previous
    finally:
        tools.reset_state()


def test_close_timeout_remains_a_retryable_cleanup_failure(tmp_path):
    class UnstoppableProcess:
        pid = 1
        def terminate(self):
            pass
        def kill(self):
            pass
        def poll(self):
            return None
        def wait(self, timeout):
            raise subprocess.TimeoutExpired('fake shell', timeout)

    session = ShellSession.__new__(ShellSession)
    session._lifecycle_lock = RLock()
    session._closed = False
    session.process = UnstoppableProcess()
    from saiworks.tools.process_lifecycle import OwnedProcess
    session._process_owner = OwnedProcess(session.process)
    tools = registry(tmp_path)
    tools.attach_state('shell_session', session)
    with pytest.raises(ExceptionGroup):
        tools.reset_state()
    assert tools.state_for('shell_session') is session
    assert session.closed


def test_resource_replaced_inside_close_is_retained_for_next_cleanup(tmp_path):
    tools = registry(tmp_path)
    calls = []
    class Replacement:
        def close(self):
            calls.append('replacement')
    replacement = Replacement()
    class Original:
        def close(self):
            calls.append('original')
            tools.attach_state('resource', replacement)
    tools.attach_state('resource', Original())
    with pytest.raises(ExceptionGroup):
        tools.reset_state()
    assert tools.state_for('resource') is replacement
    assert calls == ['original']
    tools.reset_state()
    assert calls == ['original', 'replacement']
    assert tools.state_for('resource') is None


def test_cleanup_kills_child_that_ignores_term_even_if_shell_exits_first(tmp_path):
    import os
    import shlex
    import signal
    import sys
    import time
    child_script = tmp_path / 'ignore_term.py'
    child_script.write_text(
        "import pathlib,signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path('ready').write_text('ready')\n"
        "time.sleep(30)\n"
    )
    tools = registry(tmp_path)
    command = (f'{shlex.quote(sys.executable)} {shlex.quote(str(child_script))} & '
               'child=$!; while [ ! -f ready ]; do sleep 0.01; done; printf %s "$child"')
    result = tools.execute(ToolAction('shell_exec', {'command': command}), cwd=str(tmp_path))
    assert result.success
    child = int(result.metadata['stdout'])
    def live():
        try:
            data = Path(f'/proc/{child}/stat').read_text()
        except FileNotFoundError:
            return False
        return data[data.rfind(')') + 2:].split()[0] not in {'Z', 'X'}
    try:
        tools.reset_state()
        deadline = time.monotonic() + 1
        while live() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not live()
    finally:
        if live():
            os.kill(child, signal.SIGKILL)
        tools.reset_state()
