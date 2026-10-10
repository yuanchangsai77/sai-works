from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .sessions.tasks import TaskSnapshot


class TaskAccess:
    """Identity-only capability issued by the trusted local Runtime owner."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class TaskUpdate:
    snapshot: TaskSnapshot
    from_revision: int | None
    to_revision: int
    resync_required: bool


class TaskWatch:
    """Bounded pull observer: only the last revision is retained."""

    def __init__(self, runtime, access: TaskAccess, after_revision: int | None):
        self._runtime = runtime
        self._access = access
        self._revision = after_revision
        self._closed = False
        self._lock = Lock()

    def poll(self) -> TaskUpdate | None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Task watch is closed")
            snapshot = self._runtime.snapshot(self._access)
            if self._revision == snapshot.revision:
                return None
            update = TaskUpdate(snapshot, self._revision, snapshot.revision,
                                self._revision is not None and snapshot.revision != self._revision + 1)
            self._revision = snapshot.revision
            return update

    def close(self) -> None:
        with self._lock:
            self._closed = True
