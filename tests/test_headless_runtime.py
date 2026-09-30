import builtins
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from saiworks.app import create_runtime
from saiworks.runtime import Runtime
from saiworks.sessions.tasks import TaskStore
from saiworks.types import ExecutionSummary, ModelReply, ToolAction, UserRequest


def test_headless_factory_and_persistence_do_not_load_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert "interaction" not in name, name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    runtime = create_runtime(workspace_root=tmp_path)
    assert runtime.engine.approval_callback is None
    assert runtime.engine.progress_reporter is None
    session = runtime.session_store.create(str(tmp_path))
    request = UserRequest("inspect workspace", str(tmp_path), {"session_id": session.session_id})
    summary = runtime.execute(request, submission_id="headless")
    runtime.persist_run(session, request.prompt, summary, status="closed", close_runtime=True)
    record = runtime.task_store.get_submission("headless")
    assert record.task_id == summary.checkpoint.task_id
    assert record.state == "finished"
    assert record.result.final_message == summary.final_message
    assert runtime.load_session(session.session_id).messages[-1]["content"] == summary.final_message
    assert capsys.readouterr().out == ""


def test_duplicate_submission_and_restart_never_repeat_execution(tmp_path):
    class Engine:
        calls = 0

        def execute(self, _request):
            self.calls += 1
            return ExecutionSummary("done", [])

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = Engine()
    runtime = Runtime(engine, task_store=store)
    request = UserRequest("write once", str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: runtime.execute(request, submission_id="once"), range(8)))
    assert engine.calls == 1
    assert all(result.final_message == "done" for result in results)
    restarted = Runtime(engine, task_store=TaskStore(store.path))
    assert restarted.execute(request, submission_id="once").final_message == "done"
    assert engine.calls == 1
    with pytest.raises(ValueError, match="different input"):
        restarted.execute(UserRequest("different", str(tmp_path)), submission_id="once")
    assert engine.calls == 1


def test_accepted_input_is_frozen_and_running_claim_is_not_replayed(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    request = UserRequest("initial", str(tmp_path), {"context_paths": ["a.txt"]})
    record = store.accept(request, submission_id="accepted")
    request.metadata["context_paths"].append("b.txt")
    assert record.request().metadata["context_paths"] == ["a.txt"]
    assert store.claim("accepted")
    restarted = TaskStore(store.path)
    assert restarted.get_submission("accepted").task_id == record.task_id
    assert not restarted.claim("accepted")


def test_failed_acceptance_prevents_execution(tmp_path, monkeypatch):
    class Engine:
        calls = 0

        def execute(self, _request):
            self.calls += 1
            return ExecutionSummary("done", [])

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = Engine()
    runtime = Runtime(engine, task_store=store)
    monkeypatch.setattr(store, "accept", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        runtime.execute(UserRequest("write", str(tmp_path)), submission_id="failed")
    assert engine.calls == 0
    assert store.get_submission("failed") is None


def test_single_slot_fifo_and_failure_release(tmp_path):
    first_started, release_first = Event(), Event()
    calls = []

    class Engine:
        def execute(self, request):
            calls.append(request.prompt)
            if request.prompt == "first":
                first_started.set()
                assert release_first.wait(3)
                raise ValueError("failed first")
            return ExecutionSummary(request.prompt, [])

    runtime = Runtime(Engine())
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(runtime.execute, UserRequest("first", str(tmp_path)))
        assert first_started.wait(3)
        second = pool.submit(runtime.execute, UserRequest("second", str(tmp_path)))
        with runtime._condition:
            assert runtime._condition.wait_for(lambda: len(runtime._queue) == 1, timeout=3)
        third = pool.submit(runtime.execute, UserRequest("third", str(tmp_path)))
        with runtime._condition:
            assert runtime._condition.wait_for(lambda: len(runtime._queue) == 2, timeout=3)
        assert calls == ["first"]
        release_first.set()
        with pytest.raises(ValueError):
            first.result(timeout=3)
        assert second.result(timeout=3).final_message == "second"
        assert third.result(timeout=3).final_message == "third"
    assert calls == ["first", "second", "third"]


def test_interrupt_retains_partial_results_and_next_run_has_new_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sample.txt").write_text("sample")
    runtime = create_runtime(workspace_root=tmp_path)
    contexts = []

    class Model:
        count = 0

        def respond(self, session):
            contexts.append(runtime.engine._execution_context)
            self.count += 1
            if self.count == 1:
                return ModelReply("reading", [ToolAction("read_file", {"path": "sample.txt"})])
            raise KeyboardInterrupt

    runtime.engine.model = Model()
    with pytest.raises(KeyboardInterrupt):
        runtime.execute(UserRequest("inspect sample", str(tmp_path)), submission_id="interrupt")
    record = runtime.task_store.get_submission("interrupt")
    assert record.state == "cancelled"
    assert record.result.outcome == "interrupted"
    assert record.result.tool_results[0].name == "read_file"
    assert record.result.checkpoint.task_id == record.task_id
    assert runtime.engine.last_failure_summary is runtime.engine._finish(ExecutionSummary("duplicate", []))
    old_context = contexts[0]
    assert old_context.cancellation.is_set()

    class FinishedModel:
        def respond(self, _session):
            return ModelReply("finished", done=True)

    runtime.engine.model = FinishedModel()
    summary = runtime.execute(UserRequest("hello", str(tmp_path)), submission_id="next")
    assert summary.outcome == "completed"
    assert runtime.engine._execution_context is not old_context
    assert not runtime.engine._execution_context.cancellation.is_set()


def test_task_tool_instances_and_observations_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)
    runtime = create_runtime(workspace_root=tmp_path)
    registries = []
    tokens = []

    class Model:
        def respond(self, _session):
            registry = runtime.engine.tools
            registries.append(registry)
            tokens.append(runtime.engine._execution_context.cancellation)
            assert registry.state_for("task_marker") is None
            registry.attach_state("task_marker", object())
            return ModelReply("finished", done=True)

    runtime.engine.model = Model()
    for identity in ("first", "second"):
        runtime.execute(UserRequest("hello", str(tmp_path), {"session_id": "same-session"}), submission_id=identity)
    assert registries[0] is not registries[1]
    assert registries[0]._tools["read_file"] is not registries[1]._tools["read_file"]
    assert tokens[0] is not tokens[1]


def test_display_failure_cannot_change_execution_or_terminal_state(tmp_path):
    class Engine:
        def execute(self, _request):
            return ExecutionSummary("authoritative result", [])

    runtime = Runtime(Engine(), task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    runtime.on_run_started = lambda _: (_ for _ in ()).throw(RuntimeError("disconnected"))
    runtime.on_run_finished = runtime.on_run_started
    summary = runtime.execute(UserRequest("hello", str(tmp_path)), submission_id="observer")
    assert summary.final_message == "authoritative result"
    assert runtime.task_store.get_submission("observer").state == "finished"


def test_bound_recovery_metadata_and_result_roundtrip(tmp_path):
    from saiworks.capabilities.model import InstructionContent
    from saiworks.types import EvidenceRecord, RuntimeBlocker, SessionResumeState, TaskCheckpoint, WorkspaceSessionState

    checkpoint = TaskCheckpoint(task_id="previous", phase="blocked", evidence=[EvidenceRecord("read", "reader", "previous", 0)])
    request = UserRequest("continue", str(tmp_path), {
        "resume_state": SessionResumeState(checkpoint=checkpoint, last_outcome="blocked"),
        "workspace_state": WorkspaceSessionState(str(tmp_path), str(tmp_path)),
    })
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = store.accept(request, submission_id="roundtrip", task_id="previous")
    bound = record.request()
    assert isinstance(bound.metadata["resume_state"], SessionResumeState)
    assert bound.metadata["resume_state"].checkpoint.evidence[0].producer == "reader"
    assert store.claim("roundtrip")
    summary = ExecutionSummary("blocked", [], checkpoint=checkpoint,
                               blockers=[RuntimeBlocker("approval_required", "needs approval")],
                               active_instructions=[InstructionContent("skill:test", "Test", "instructions")])
    store.finish("roundtrip", summary)
    restored = store.get_submission("roundtrip").result
    assert restored.checkpoint.evidence[0].task_id == "previous"
    assert restored.blockers[0].error_code == "approval_required"
    assert isinstance(restored.active_instructions[0], InstructionContent)


def test_background_result_is_committed_only_after_handoff_validation(tmp_path):
    class Engine:
        def execute(self, _request):
            return ExecutionSummary("candidate", [])

    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(Engine(), task_store=store)
    summary = runtime.execute(UserRequest("delegate", str(tmp_path), {"defer_finalize": True}),
                              background=True, submission_id="child")
    assert store.get_submission("child").state == "running"
    assert store.get_submission("child").result is None
    summary.outcome, summary.final_message = "stalled", "handoff evidence missing"
    runtime.persist_run(None, "delegate", summary)
    assert store.get_submission("child").result.outcome == "stalled"
    assert store.get_submission("child").result.final_message == "handoff evidence missing"


def test_headless_compaction_archives_history_and_rolls_back_failed_save(tmp_path, monkeypatch):
    from saiworks.sessions import SessionStore

    store = SessionStore(base_dir=tmp_path)
    messages = [{"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}]
    session = store.create(str(tmp_path), messages=messages)
    runtime = Runtime(None, session_store=store)
    conversation = list(messages)
    result = runtime.compact_session(session, conversation)
    assert result.old_count == 2 and result.new_count == 1
    assert store.load_archived_messages(session.session_id, result.archive_id) == messages
    assert session.context_generation == 1
    before = list(conversation)
    generation = session.context_generation
    monkeypatch.setattr(store, "save", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        runtime.compact_session(session, conversation)
    assert session.context_generation == generation
    assert conversation == before


def test_acceptance_commit_failure_rolls_back_identity_and_prevents_execution(tmp_path, monkeypatch):
    import sqlite3

    class Engine:
        calls = 0

        def execute(self, _request):
            self.calls += 1
            return ExecutionSummary("done", [])

    store = TaskStore(tmp_path / "tasks.sqlite3")
    engine = Engine()
    runtime = Runtime(engine, task_store=store)
    connect = store._connect

    def failed_commit_connection():
        connection = connect()
        connection.set_authorizer(
            lambda action, operation, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT"
            else sqlite3.SQLITE_OK
        )
        return connection

    monkeypatch.setattr(store, "_connect", failed_commit_connection)
    with pytest.raises(sqlite3.DatabaseError):
        runtime.execute(UserRequest("mutate", str(tmp_path)), submission_id="failed-commit")
    assert engine.calls == 0
    assert store.get_submission("failed-commit") is None


def test_query_projection_cannot_modify_real_policy_or_execute_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)
    runtime = create_runtime(workspace_root=tmp_path)
    mode = runtime.safety_mode()
    view = runtime.view()
    view.guardrails.policy.mode = "auto"
    assert runtime.safety_mode() == mode
    assert not hasattr(view.model, "respond")
    assert not hasattr(view.tools, "execute")
    assert not hasattr(view.capability_warehouse, "activate")
