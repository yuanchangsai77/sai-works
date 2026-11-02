import os
import subprocess
import sys

import pytest

from saiworks.tools.process_lifecycle import OwnedProcess, supports_process_groups


@pytest.mark.skipif(not supports_process_groups(), reason='Requires local Linux group ownership verification')
def test_group_cleanup_timeout_keeps_leader_unreaped_for_safe_retry(monkeypatch):
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    owner = OwnedProcess(process, owns_group=True)
    actual = OwnedProcess._group_active
    monkeypatch.setattr(OwnedProcess, '_group_active', lambda _: True)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            owner.close(grace=0.01)
        assert process.returncode is None
    finally:
        monkeypatch.setattr(OwnedProcess, '_group_active', actual)
        owner.close()
    assert process.returncode is not None
    owner.close()


def test_reaped_leader_never_authorizes_signalling_a_potentially_reused_group(monkeypatch):
    class ReapedProcess:
        pid = 12345
        returncode = 0
    signals = []
    monkeypatch.setattr(os, 'killpg', lambda *args: signals.append(args))
    with pytest.raises(RuntimeError, match='reaped before group cleanup'):
        OwnedProcess(ReapedProcess(), owns_group=True).close()
    assert signals == []


@pytest.mark.skipif(not supports_process_groups(), reason='Requires local Linux group ownership verification')
def test_exit_inspection_cannot_race_the_final_reap(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
    owner = OwnedProcess(process, owns_group=True)
    reaped, release, inspecting = Event(), Event(), Event()
    wait = process.wait
    def paused_wait(*args, **kwargs):
        value = wait(*args, **kwargs)
        reaped.set()
        assert release.wait(3)
        return value
    monkeypatch.setattr(process, 'wait', paused_wait)
    def inspect():
        inspecting.set()
        return owner.leader_exited()
    with ThreadPoolExecutor(max_workers=2) as executor:
        closing = executor.submit(owner.close)
        assert reaped.wait(3)
        inspection = executor.submit(inspect)
        assert inspecting.wait(3)
        release.set()
        closing.result(timeout=3)
        assert inspection.result(timeout=3) is True
