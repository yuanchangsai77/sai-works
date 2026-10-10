import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from saiworks.runtime import Runtime
from saiworks.runtime_observation import TaskAccess
from saiworks.sessions import SessionStore
from saiworks.sessions.tasks import TaskStore
from saiworks.types import ExecutionSummary, UserRequest


def test_snapshot_aggregates_submissions_and_preserves_attempt_results(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    request = UserRequest("first", str(tmp_path))
    store.accept(request, submission_id="first", task_id="task")
    store.accept(UserRequest("second", str(tmp_path)), submission_id="second", task_id="task")
    assert store.snapshot("task").revision == 2
    store.accept(request, submission_id="first", task_id="task")
    assert store.snapshot("task").revision == 2
    assert store.claim("first")
    assert not store.claim("first")
    assert store.snapshot("task").current_submission_id == "first"
    store.finish("first", ExecutionSummary("first result", []), error="earlier error")
    snapshot = store.snapshot("task")
    assert snapshot.revision == 4
    assert snapshot.state == "queued"
    assert snapshot.current_submission_id == "second"
    assert snapshot.submissions[0].error == "earlier error"
    assert snapshot.submissions[1].error is None
    snapshot.submissions[0].result.final_message = "changed display"
    assert store.snapshot("task").submissions[0].result.final_message == "first result"
    store.claim("second")
    store.finish("second", ExecutionSummary("second result", []))
    restarted = TaskStore(store.path).snapshot("task")
    assert restarted.revision == 6
    assert restarted.state == "finished"
    assert [record.submission_id for record in restarted.submissions] == ["first", "second"]


def test_observers_resync_without_blocking_producer_and_reject_invalid_access(tmp_path):
    class Engine:
        def execute(self, _request):
            return ExecutionSummary("done", [])

    runtime = Runtime(Engine(), task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    request = UserRequest("work", str(tmp_path))
    task = runtime.accept(request, submission_id="work")
    access = runtime.grant_task_access(task.task_id)
    fast = runtime.watch(access)
    slow = runtime.watch(access, after_revision=1)
    assert fast.poll().snapshot.state == "queued"
    assert fast.poll() is None
    runtime.task_store.claim("work")
    assert fast.poll().resync_required is False
    runtime.task_store.finish("work", ExecutionSummary("done", []))
    fast_update, slow_update = fast.poll(), slow.poll()
    assert fast_update.snapshot == slow_update.snapshot
    assert fast_update.resync_required is False
    assert slow_update.resync_required is True
    assert slow_update.from_revision == 1
    assert slow_update.to_revision == 3
    with pytest.raises(PermissionError):
        runtime.snapshot(TaskAccess())
    other = Runtime(Engine(), task_store=runtime.task_store)
    with pytest.raises(PermissionError):
        other.snapshot(access)
    for revision in (-1, 4, True, "1"):
        with pytest.raises(ValueError):
            runtime.watch(access, revision)
    closed = runtime.watch(access)
    closed.close()
    with pytest.raises(RuntimeError, match="closed"):
        closed.poll()
    runtime.revoke_task_access(access)
    with pytest.raises(PermissionError):
        fast.poll()


def test_snapshot_watch_handoff_observes_update_and_runtime_close_revokes(tmp_path):
    runtime = Runtime(object(), task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="work")
    access = runtime.grant_task_access(task.task_id)
    snapshot = runtime.snapshot(access)
    runtime.task_store.claim("work")
    watch = runtime.watch(access, snapshot.revision)
    assert watch.poll().snapshot.state == "running"
    runtime.close()
    with pytest.raises(PermissionError):
        watch.poll()


def test_revision_failure_rolls_back_state_change(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = store.accept(UserRequest("work", str(tmp_path)), submission_id="work")
    before = store.snapshot(task.task_id)
    monkeypatch.setattr(store, "_bump", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        store.claim("work")
    assert store.snapshot(task.task_id) == before


def test_delivery_marker_failure_suppresses_notification_after_session_save(tmp_path, monkeypatch):
    class Engine:
        calls = 0

        def execute(self, _request):
            self.calls += 1
            return ExecutionSummary("saved", [])

    sessions = SessionStore(base_dir=tmp_path / "sessions")
    session = sessions.create(str(tmp_path))
    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = Engine()
    runtime = Runtime(engine, session_store=sessions, task_store=store)
    request = UserRequest("work", str(tmp_path), {"session_id": session.session_id})
    set_delivery = store.set_delivery_state

    def fail_completion(submission_id, state):
        if state == "completed":
            raise OSError("marker unavailable")
        return set_delivery(submission_id, state)

    monkeypatch.setattr(store, "set_delivery_state", fail_completion)
    observed = []
    conversation = []
    runtime.on_run_finished = observed.append
    with pytest.raises(OSError, match="marker unavailable"):
        runtime.execute_session_turn(request, request.prompt, session, conversation, 0)
    record = runtime.get_submission(request.metadata["runtime_submission_id"])
    assert record.state == "finished"
    assert record.delivery_state == "pending"
    assert sessions.load(session.session_id).messages[-1]["content"] == "saved"
    assert observed == []
    monkeypatch.setattr(store, "set_delivery_state", set_delivery)
    runtime.execute_session_turn(request, request.prompt, session, conversation, 0)
    assert engine.calls == 1
    assert len(conversation) == 2
    assert len(sessions.load(session.session_id).messages) == 2
    assert store.get_submission(record.submission_id).delivery_state == "completed"
    assert len(observed) == 1
    runtime.execute_session_turn(request, request.prompt, session, conversation, 0)
    assert len(conversation) == 2
    assert len(observed) == 1


def test_slow_snapshot_does_not_hold_scheduler_lock_and_revocation_wins(tmp_path, monkeypatch):
    runtime = Runtime(object(), task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="work")
    access = runtime.grant_task_access(task.task_id)
    entered, release = Event(), Event()
    snapshot = runtime.task_store.snapshot

    def slow_snapshot(task_id):
        entered.set()
        assert release.wait(3)
        return snapshot(task_id)

    monkeypatch.setattr(runtime.task_store, "snapshot", slow_snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(runtime.snapshot, access)
        assert entered.wait(3)
        try:
            control = pool.submit(runtime.revoke_task_access, access)
            control.result(timeout=1)
        finally:
            release.set()
        with pytest.raises(PermissionError):
            reading.result(timeout=3)


@pytest.mark.parametrize("invalid_access", [None, "task", {}, []])
def test_malformed_observation_access_is_rejected(tmp_path, invalid_access):
    runtime = Runtime(object(), task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    with pytest.raises(PermissionError):
        runtime.snapshot(invalid_access)
    with pytest.raises(PermissionError):
        runtime.revoke_task_access(invalid_access)


def test_persisted_run_marker_retry_does_not_repeat_session_save(tmp_path, monkeypatch):
    class Engine:
        calls = 0

        def execute(self, _request):
            self.calls += 1
            return ExecutionSummary("saved", [])

    sessions = SessionStore(base_dir=tmp_path / "sessions")
    session = sessions.create(str(tmp_path))
    engine = Engine()
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, session_store=sessions, task_store=store)
    request = UserRequest("work", str(tmp_path), {"session_id": session.session_id})
    original = store.set_delivery_state

    def fail_marker(submission_id, state):
        if state == "completed":
            raise OSError("marker unavailable")
        return original(submission_id, state)

    monkeypatch.setattr(store, "set_delivery_state", fail_marker)
    with pytest.raises(OSError):
        runtime.execute_persisted_run(request, session)
    before = sessions.load(session.session_id)
    monkeypatch.setattr(store, "set_delivery_state", original)
    runtime.execute_persisted_run(request, session)
    after = sessions.load(session.session_id)
    assert engine.calls == 1
    assert after.revision == before.revision
    assert after.messages == before.messages


def test_migration_commit_failure_preserves_old_database(tmp_path, monkeypatch):
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE tasks (
            submission_id TEXT PRIMARY KEY, task_id TEXT, session_id TEXT,
            request_fingerprint TEXT, bound_context TEXT, state TEXT,
            run_ids TEXT, result TEXT, error TEXT, schema_version INTEGER DEFAULT 1)""")
    connect = TaskStore._connect

    def fail_commit(self):
        connection = connect(self)
        connection.set_authorizer(lambda action, operation, *_:
            sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT"
            else sqlite3.SQLITE_OK)
        return connection

    monkeypatch.setattr(TaskStore, "_connect", fail_commit)
    with pytest.raises(sqlite3.DatabaseError):
        TaskStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "delivery_state" not in {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='task_revisions'").fetchone() is None


def test_session_save_failure_remains_pending_and_retry_does_not_execute(tmp_path, monkeypatch):
    class Engine:
        calls = 0

        def execute(self, _request):
            self.calls += 1
            return ExecutionSummary("result", [])

    sessions = SessionStore(base_dir=tmp_path / "sessions")
    session = sessions.create(str(tmp_path))
    engine = Engine()
    runtime = Runtime(engine, session_store=sessions, task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    request = UserRequest("work", str(tmp_path), {"session_id": session.session_id})
    save = sessions.save
    monkeypatch.setattr(sessions, "save", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        runtime.execute_session_turn(request, request.prompt, session, [], 0)
    task = runtime.get_submission(request.metadata["runtime_submission_id"])
    access = runtime.grant_task_access(task.task_id)
    snapshot = runtime.snapshot(access)
    assert snapshot.state == "finished"
    assert snapshot.submissions[0].delivery_state == "pending"
    monkeypatch.setattr(sessions, "save", save)
    observed = []
    runtime.on_run_finished = lambda _: observed.append(runtime.snapshot(access))
    runtime.execute_session_turn(request, request.prompt, session, [], 0)
    assert engine.calls == 1
    assert observed[0].submissions[0].delivery_state == "completed"
    assert observed[0].revision > snapshot.revision


def test_upgrade_old_records_and_reject_future_database(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE tasks (
            submission_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            session_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
            bound_context TEXT NOT NULL, state TEXT NOT NULL,
            run_ids TEXT NOT NULL DEFAULT '[]', result TEXT, error TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1)""")
        connection.execute("""INSERT INTO tasks
            (submission_id, task_id, session_id, request_fingerprint, bound_context, state)
            VALUES ('old', 'task', '', 'fingerprint', '{}', 'running')""")
    store = TaskStore(path)
    snapshot = store.snapshot("task")
    assert snapshot.revision == 1
    assert snapshot.state == "running"
    assert snapshot.submissions[0].delivery_state == "unknown"
    assert snapshot.submissions[0].schema_version == 2
    assert not store.claim("old")
    assert TaskStore(path).snapshot("task") == snapshot
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=3")
    with pytest.raises(ValueError, match="database version"):
        TaskStore(path)


def test_concurrent_snapshot_reads_consistent_revision_and_state(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    task = store.accept(UserRequest("work", str(tmp_path)), submission_id="work")

    def write():
        store.claim("work")
        store.finish("work", ExecutionSummary("done", []))

    def read():
        for _ in range(20):
            snapshot = store.snapshot(task.task_id)
            assert snapshot.state == {1: "queued", 2: "running", 3: "finished"}[snapshot.revision]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(read), pool.submit(write), pool.submit(read)]
        for future in futures:
            future.result(timeout=5)


def test_two_observers_follow_runtime_execution_to_same_result(tmp_path):
    started, release = Event(), Event()

    class Engine:
        def execute(self, _request):
            started.set()
            assert release.wait(3)
            return ExecutionSummary("executed result", [])

    runtime = Runtime(Engine(), task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    request = UserRequest("work", str(tmp_path))
    record = runtime.accept(request, submission_id="work")
    watches = [runtime.watch(runtime.grant_task_access(record.task_id)) for _ in range(2)]
    assert all(watch.poll().snapshot.state == "queued" for watch in watches)
    with ThreadPoolExecutor(max_workers=1) as pool:
        execution = pool.submit(runtime.execute, request, submission_id="work")
        assert started.wait(3)
        try:
            assert all(watch.poll().snapshot.state == "running" for watch in watches)
        finally:
            release.set()
        assert execution.result(timeout=3).final_message == "executed result"
    updates = [watch.poll() for watch in watches]
    assert updates[0].snapshot == updates[1].snapshot
    assert updates[0].snapshot.state == "finished"
    assert updates[0].snapshot.submissions[0].result.final_message == "executed result"
