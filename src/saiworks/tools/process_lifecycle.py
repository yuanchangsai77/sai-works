"""Owned local-process cleanup; Linux groups are fenced until the leader is reaped."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import time
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock


def supports_process_groups() -> bool:
    return (os.name == 'posix' and Path('/proc/self/stat').is_file()
            and all(hasattr(os, name) for name in ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT')))


def _owner_locked(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


@dataclass(slots=True)
class OwnedProcess:
    process: subprocess.Popen
    owns_group: bool = False
    _stopped: bool = field(default=False, init=False)
    _lock: object = field(default_factory=RLock, init=False, repr=False)

    @_owner_locked
    def leader_exited(self) -> bool:
        if self._stopped:
            return True
        if not self.owns_group:
            return self.process.poll() is not None
        if self.process.returncode is not None:
            raise RuntimeError('Process leader was reaped before group cleanup; ownership cannot be verified')
        try:
            status = os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError as error:
            raise RuntimeError('Process leader ownership cannot be verified') from error
        return status is not None

    def _signal_group(self, sig: int) -> None:
        self.leader_exited()  # Verify ownership without reaping the leader.
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass

    def _group_active(self) -> bool:
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                data = (entry / 'stat').read_text()
            except FileNotFoundError:
                continue
            fields = data[data.rfind(')') + 2:].split()
            if len(fields) < 4:
                raise RuntimeError('Cannot inspect process-group membership')
            if (fields[2] == str(self.process.pid) and fields[3] == str(self.process.pid)
                    and fields[0] not in {'Z', 'X'}):
                return True
        return False

    @_owner_locked
    def close(self, grace: float = 0.5) -> None:
        if self._stopped:
            return
        if not self.owns_group:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=grace)
            self._stopped = True
            return
        self._signal_group(signal.SIGTERM)
        deadline = time.monotonic() + grace
        while not self.leader_exited() and time.monotonic() < deadline:
            time.sleep(0.01)
        # The unreaped leader reserves the numeric group identity during escalation.
        self._signal_group(signal.SIGKILL)
        deadline = time.monotonic() + grace
        while self._group_active():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('process group cleanup', grace)
            time.sleep(0.01)
        self.process.wait(timeout=grace)
        self._stopped = True
