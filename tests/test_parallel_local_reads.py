from threading import Barrier, Event, enumerate as live_threads, get_ident

import pytest

from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy, PolicyDecision
from saiworks.tools.base import SimpleTool
from saiworks.tools.builtin_provider import build_builtin_registry
from saiworks.types import ModelReply, ToolAction, ToolResult, UserRequest


def make_engine(tmp_path, actions):
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    tools = build_builtin_registry(logger)

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            if self.calls == 1:
                return ModelReply(message="inspect", actions=actions)
            return ModelReply(message="done", done=True)

    engine = ExecutionEngine(
        model=Model(), tools=tools, logger=logger,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
    )
    request = UserRequest(prompt="inspect files", cwd=str(tmp_path), metadata={"session_id": "read-test"})
    return engine, request


def test_read_handlers_overlap_but_observations_and_logs_remain_ordered(tmp_path, monkeypatch):
    path = tmp_path / "source.txt"
    path.write_text("first\nsecond\n", encoding="utf-8")
    actions = [ToolAction("read_file", {"path": "source.txt", "start_line": line, "end_line": line})
               for line in (1, 2)]
    engine, request = make_engine(tmp_path, actions)
    reader = engine.tools._tools["read_file"]
    original = reader.handler
    barrier, second_done = Barrier(2), Event()
    finished = []
    main_thread = get_ident()

    def read(action, context):
        assert get_ident() != main_thread
        with pytest.raises(RuntimeError, match="already executing"):
            engine.tools.execute(ToolAction("file_info", {"path": "source.txt"}), cwd=str(tmp_path))
        barrier.wait(timeout=3)
        if action.arguments["start_line"] == 1:
            assert second_done.wait(timeout=3)
        result = original(action, context)
        finished.append(action.arguments["start_line"])
        if action.arguments["start_line"] == 2:
            second_done.set()
        return result

    monkeypatch.setattr(reader, "handler", read)
    log = engine.logger.record

    def record(name, payload):
        assert get_ident() == main_thread
        return log(name, payload)

    monkeypatch.setattr(engine.logger, "record", record)
    summary = engine.execute(request)
    assert finished == [2, 1]
    assert [result.output for result in summary.tool_results] == ["first\n", "second\n"]
    observations = engine.tools.state_for("read_files")[str(path)]["observations"]
    assert [item["lines"] for item in observations] == [{1: "first"}, {2: "second"}]
    assert [item["observation_id"] for item in observations] == [
        result.metadata["_observation"]["identifier"] for result in summary.tool_results
    ]


def test_mutation_is_a_barrier_and_invalidates_read_cache(tmp_path, monkeypatch):
    for name in ("a", "b"):
        (tmp_path / name).write_text("old", encoding="utf-8")
    actions = [ToolAction("read_file", {"path": name}) for name in ("a", "b")]
    actions += [ToolAction("mutate", {}), ToolAction("read_file", {"path": "a"})]
    engine, request = make_engine(tmp_path, actions)
    reader = engine.tools._tools["read_file"]
    original = reader.handler
    barrier, written = Barrier(2), Event()
    finished = []

    def read(action, context):
        if not written.is_set():
            barrier.wait(timeout=3)
        result = original(action, context)
        finished.append(action.arguments["path"])
        return result

    def mutate(action, context):
        assert sorted(finished) == ["a", "b"]
        (tmp_path / "a").write_text("new", encoding="utf-8")
        written.set()
        return ToolResult(action.name, True, "changed")

    monkeypatch.setattr(reader, "handler", read)
    engine.tools.register(SimpleTool("mutate", "Mutate", {}, mutate, risk_level="write"))
    summary = engine.execute(request)
    assert [result.output for result in summary.tool_results] == ["old", "old", "changed", "new"]


def test_duplicate_reads_remain_deduplicated(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("hello", encoding="utf-8")
    action = ToolAction("read_file", {"path": "a"})
    engine, request = make_engine(tmp_path, [action, action])
    reader = engine.tools._tools["read_file"]
    original = reader.handler
    calls = []

    def read(action, context):
        calls.append(action)
        return original(action, context)

    monkeypatch.setattr(reader, "handler", read)
    summary = engine.execute(request)
    assert len(calls) == 1
    assert summary.tool_results[1].metadata["duplicate"]
    assert summary.tool_results[1].metadata["skipped"]


def test_policy_denial_after_pending_read_is_ordered_and_never_executed(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("allowed", encoding="utf-8")
    actions = [ToolAction("read_file", {"path": path}) for path in ("a", "denied")]
    engine, request = make_engine(tmp_path, actions)

    class Policy(DefaultPolicy):
        def evaluate(self, action, definition=None):
            if action.arguments.get("path") == "denied":
                return PolicyDecision(False, False, "denied", "read")
            return super().evaluate(action, definition)

    engine.guardrails.policy = Policy(mode="auto")
    summary = engine.execute(request)
    assert summary.tool_results[0].output == "allowed"
    assert summary.tool_results[1].error_code == "blocked_by_policy"
    executed = [event for event in engine.logger.events if event.name == "tool.execute"]
    assert len(executed) == 1


def test_worker_failure_commits_sibling_and_releases_execution_lock(tmp_path, monkeypatch):
    (tmp_path / "b").write_text("sibling", encoding="utf-8")
    engine, request = make_engine(tmp_path, [ToolAction("read_file", {"path": path}) for path in ("a", "b")])
    reader = engine.tools._tools["read_file"]
    original = reader.handler
    barrier = Barrier(2)

    def read(action, context):
        barrier.wait(timeout=3)
        if action.arguments["path"] == "a":
            raise RuntimeError("read failed")
        return original(action, context)

    monkeypatch.setattr(reader, "handler", read)
    with pytest.raises(RuntimeError, match="read failed"):
        engine.execute(request)
    assert [result.output for result in engine.last_failure_summary.tool_results] == ["sibling"]
    monkeypatch.setattr(reader, "handler", original)
    assert engine.tools.execute(ToolAction("read_file", {"path": "b"}), cwd=str(tmp_path)).success


@pytest.mark.parametrize("cleanup_during_read", [False, True])
def test_cancellation_drains_dispatched_reads_before_stopping(tmp_path, monkeypatch, cleanup_during_read):
    for path in ("a", "b"):
        (tmp_path / path).write_text(path, encoding="utf-8")
    engine, request = make_engine(tmp_path, [ToolAction("read_file", {"path": path}) for path in ("a", "b")])
    reader = engine.tools._tools["read_file"]
    original = reader.handler
    barrier = Barrier(2)

    def read(action, context):
        barrier.wait(timeout=3)
        if action.arguments["path"] == "a" and cleanup_during_read:
            engine.cancel_current_run()
        else:
            engine._execution_context.cancellation.set()
        return original(action, context)

    monkeypatch.setattr(reader, "handler", read)
    with pytest.raises(KeyboardInterrupt):
        engine.execute(request)
    assert engine.last_failure_summary.outcome == "interrupted"
    assert [result.output for result in engine.last_failure_summary.tool_results] == ["a", "b"]
    assert engine.tools.state_for("read_files") is None
    assert not any(thread.name.startswith("saiworks-read") for thread in live_threads())


def test_read_risk_alone_does_not_enable_concurrency(tmp_path):
    main_thread = get_ident()
    engine, request = make_engine(tmp_path, [ToolAction("extension", {"index": i}) for i in range(2)])
    threads = []

    def read(action, context):
        threads.append(get_ident())
        return ToolResult(action.name, True, str(action.arguments["index"]))

    engine.tools.register(SimpleTool("extension", "Unknown extension", {}, read))
    summary = engine.execute(request)
    assert len(summary.tool_results) == 2
    assert threads == [main_thread, main_thread]


def test_read_batch_is_bounded_to_four(tmp_path, monkeypatch):
    actions = []
    for index in range(6):
        (tmp_path / str(index)).write_text(str(index), encoding="utf-8")
        actions.append(ToolAction("read_file", {"path": str(index)}))
    engine, request = make_engine(tmp_path, actions)
    reader = engine.tools._tools["read_file"]
    original = reader.handler
    first, second = Barrier(4), Barrier(2)

    def read(action, context):
        (first if int(action.arguments["path"]) < 4 else second).wait(timeout=3)
        return original(action, context)

    monkeypatch.setattr(reader, "handler", read)
    summary = engine.execute(request)
    assert [result.output for result in summary.tool_results] == [str(index) for index in range(6)]


def test_external_path_approval_remains_serial_and_is_reused(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("one\ntwo\n", encoding="utf-8")
    actions = [ToolAction("read_file", {"path": str(external), "start_line": line, "end_line": line})
               for line in (1, 2)]
    engine, request = make_engine(workspace, actions)
    main_thread = get_ident()
    approvals = []

    def approve(action, reason):
        assert get_ident() == main_thread
        approvals.append(action.name)
        return True

    engine.approval_callback = approve
    summary = engine.execute(request)
    assert approvals == ["workspace_access"]
    assert [result.output for result in summary.tool_results] == ["one\n", "two\n"]


def test_stat_and_listing_can_overlap(tmp_path, monkeypatch):
    (tmp_path / "a").write_text("a", encoding="utf-8")
    engine, request = make_engine(tmp_path, [
        ToolAction("file_info", {"path": "a"}), ToolAction("list_dir", {"path": "."}),
    ])
    barrier = Barrier(2)

    def wrap(original):
        def run(action, context):
            barrier.wait(timeout=3)
            return original(action, context)
        return run

    for name in ("file_info", "list_dir"):
        tool = engine.tools._tools[name]
        monkeypatch.setattr(tool, "handler", wrap(tool.handler))
    summary = engine.execute(request)
    assert all(result.success for result in summary.tool_results)
    assert [result.name for result in summary.tool_results] == ["file_info", "list_dir"]


def test_delegated_scope_rejection_never_reaches_worker(tmp_path):
    (tmp_path / "allowed").mkdir()
    (tmp_path / "allowed" / "a").write_text("allowed", encoding="utf-8")
    engine, request = make_engine(tmp_path, [
        ToolAction("read_file", {"path": "allowed/a"}),
        ToolAction("read_file", {"path": "denied"}),
    ])
    request.metadata["delegated_task"] = {"allowed_resources": ["allowed"], "allowed_effects": ["read"]}
    summary = engine.execute(request)
    assert summary.tool_results[0].output == "allowed"
    assert summary.tool_results[1].error_code == "delegated_resource_not_allowed"
    assert len([event for event in engine.logger.events if event.name == "tool.execute"]) == 1


def test_cancelled_admission_never_dispatches_or_counts_pending_reads(tmp_path):
    engine, request = make_engine(tmp_path, [ToolAction("read_file", {"path": path}) for path in ("a", "b")])

    class CancelDuringAdmission(DefaultPolicy):
        def evaluate(self, action, definition=None):
            if action.arguments.get("path") == "b":
                engine._execution_context.cancellation.set()
            return super().evaluate(action, definition)

    engine.guardrails.policy = CancelDuringAdmission(mode="auto")
    with pytest.raises(KeyboardInterrupt):
        engine.execute(request)
    assert engine.last_failure_summary.tool_results == []
    assert engine._run_state.real_tool_actions == 0
    assert not any(event.name == "tool.execute" for event in engine.logger.events)


def test_changed_target_after_admission_uses_normal_path_denial(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "local").write_text("local", encoding="utf-8")
    (workspace / "other").write_text("other", encoding="utf-8")
    external = tmp_path / "external"
    external.write_text("private", encoding="utf-8")
    link = workspace / "link"
    link.symlink_to(workspace / "local")
    engine, request = make_engine(workspace, [
        ToolAction("read_file", {"path": "link"}), ToolAction("read_file", {"path": "other"}),
    ])
    dispatch = engine.tools.execute_read_batch

    def change_target(calls, **kwargs):
        link.unlink()
        link.symlink_to(external)
        return dispatch(calls, **kwargs)

    monkeypatch.setattr(engine.tools, "execute_read_batch", change_target)
    approvals = []
    engine.approval_callback = lambda action, reason: approvals.append(action.name) or False
    summary = engine.execute(request)
    assert approvals == ["workspace_access"]
    assert summary.tool_results[0].error_code == "approval_denied"
    assert summary.tool_results[1].output == "other"
    assert all("private" not in result.output for result in summary.tool_results)


def test_cancellation_in_registry_preflight_prevents_batch_dispatch(tmp_path, monkeypatch):
    engine, request = make_engine(tmp_path, [ToolAction("read_file", {"path": path}) for path in ("a", "b")])
    preflight = engine.tools.preflight
    checks = []

    def check(action, **kwargs):
        checks.append(action)
        if len(checks) == 3:
            engine._execution_context.cancellation.set()
        return preflight(action, **kwargs)

    monkeypatch.setattr(engine.tools, "preflight", check)
    with pytest.raises(KeyboardInterrupt):
        engine.execute(request)
    assert engine.last_failure_summary.tool_results == []
    assert engine._run_state.real_tool_actions == 0
    assert not any(event.name == "tool.execute" for event in engine.logger.events)
    assert engine.tools._execution_lock.acquire(blocking=False)
    engine.tools._execution_lock.release()


def test_pending_barrier_failure_closes_every_admitted_action(tmp_path, monkeypatch):
    from saiworks.orchestration.actions import ActionFlow

    engine, request = make_engine(tmp_path, [ToolAction("read_file", {"path": path}) for path in ("a", "b")])
    steps = []
    process_one = ActionFlow._process_one

    def capture(flow, action):
        generator = process_one(flow, action)
        steps.append(generator)
        return generator

    class SerialSecondRead(DefaultPolicy):
        def evaluate(self, action, definition=None):
            if action.arguments.get("path") == "b":
                return PolicyDecision(True, False, "serialize this read", "execute")
            return super().evaluate(action, definition)

    def fail(action, context):
        raise RuntimeError("failed before barrier")

    monkeypatch.setattr(ActionFlow, "_process_one", capture)
    monkeypatch.setattr(engine.tools._tools["read_file"], "handler", fail)
    engine.guardrails.policy = SerialSecondRead(mode="auto")
    with pytest.raises(RuntimeError, match="failed before barrier"):
        engine.execute(request)
    assert len(steps) == 2
    assert all(generator.gi_frame is None for generator in steps)
