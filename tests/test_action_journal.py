from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Event

import pytest

from saiworks.orchestration.engine import ExecutionContext, ExecutionEngine
from saiworks.orchestration.action_journal import TaskActionJournal
from saiworks.observability.logger import InMemoryLogger
from saiworks.runtime import Runtime
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.sessions.tasks import TaskStore
from saiworks.tools.base import SimpleTool
from saiworks.tools.registry import ToolRegistry
from saiworks.types import ExecutionSummary, ModelReply, ToolAction, ToolResult, UserRequest


def test_cancelled_task_cannot_admit_new_action(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = store.accept(UserRequest("write", str(tmp_path)), submission_id="one")
    assert store.claim("one")
    store.accept_cancellation(record.task_id)

    with pytest.raises(KeyboardInterrupt):
        store.start_action(record.task_id, "one", "patch", {"path": "a.py"}, str(tmp_path), "write")
    assert store.unknown_actions(record.task_id) == ()


def test_unknown_effect_is_not_replayed_by_following_submission(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    first = store.accept(UserRequest("write", str(tmp_path)), submission_id="one", task_id="task")
    assert store.claim("one")
    action_id = store.start_action(first.task_id, "one", "patch", {"path": "a.py"}, str(tmp_path), "write")
    store.finish("one", ExecutionSummary("unknown", [], outcome="runtime_error"))

    reopened = TaskStore(store.path)
    second = reopened.accept(UserRequest("continue", str(tmp_path)), submission_id="two", task_id="task")
    assert reopened.claim(second.submission_id)
    with pytest.raises(RuntimeError, match="unknown outcome"):
        reopened.start_action("task", "two", "patch", {"path": "a.py"}, str(tmp_path), "write")
    assert reopened.unknown_actions("task")[0]["action_id"] == action_id
    assert reopened.start_action("task", "two", "read_file", {"path": "a.py"}, str(tmp_path), "read") is None
    assert len(reopened.unknown_actions("task")) == 1
    reopened.finish("two", ExecutionSummary("still unknown", [], outcome="runtime_error"))
    reopened.resolve_action(action_id, effect_state="not_performed")
    third = reopened.accept(UserRequest("retry", str(tmp_path)), submission_id="three", task_id="task")
    assert reopened.claim(third.submission_id)
    retry_id = reopened.start_action("task", "three", "patch", {"path": "a.py"}, str(tmp_path), "write")
    reopened.finish_action(retry_id, success=True, error_code=None)
    assert reopened.unknown_actions("task") == ()


def test_confirmed_effect_cannot_be_replayed(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = store.accept(UserRequest("write", str(tmp_path)), submission_id="one")
    assert store.claim("one")
    action_id = store.start_action(record.task_id, "one", "patch", {"path": "a.py"}, str(tmp_path), "write")
    store.finish("one", ExecutionSummary("unknown", [], outcome="runtime_error"))
    store.resolve_action(action_id, effect_state="performed")
    second = store.accept(UserRequest("retry", str(tmp_path)), submission_id="two", task_id=record.task_id)
    assert store.claim(second.submission_id)
    with pytest.raises(RuntimeError, match="confirmed performed"):
        store.start_action(record.task_id, "two", "patch", {"path": "a.py"}, str(tmp_path), "write")
    assert store.unknown_actions(record.task_id) == ()


def test_uncertain_tool_result_blocks_changed_timeout_and_other_effects(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = store.accept(UserRequest("send", str(tmp_path)), submission_id="one")
    assert store.claim("one")
    journal = TaskActionJournal(store, record.task_id, "one")
    action = ToolAction("shell_exec", {"command": "send", "timeout": 1})
    action_id = journal.start(action, str(tmp_path), "execute")
    journal.finish(action_id, ToolResult("shell_exec", False, "timed out", "timeout"))
    unknown = store.unknown_actions(record.task_id)
    assert len(unknown) == 1
    assert unknown[0]["state"] == "uncertain"
    assert unknown[0]["error_code"] == "timeout"
    with pytest.raises(RuntimeError, match="unknown outcome"):
        store.start_action(record.task_id, "one", "shell_exec", {"command": "send", "timeout": 2}, str(tmp_path), "execute")
    with pytest.raises(RuntimeError, match="unknown outcome"):
        store.start_action(record.task_id, "one", "patch", {"path": "other.py"}, str(tmp_path), "write")
    assert store.start_action(record.task_id, "one", "read_file", {"path": "status.txt"}, str(tmp_path), "read") is None


@pytest.mark.parametrize("error_code", ["timeout", "shell_protocol_error", "mcp_transport_read_timeout"])
def test_store_keeps_ambiguous_results_uncertain_for_every_caller(tmp_path, error_code):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = store.accept(UserRequest("effect", str(tmp_path)), submission_id="one")
    assert store.claim("one")
    action_id = store.start_action(record.task_id, "one", "external_tool", {}, str(tmp_path), "execute")
    store.finish_action(action_id, success=False, error_code=error_code)
    assert store.unknown_actions(record.task_id)[0]["state"] == "uncertain"


def test_nonzero_shell_and_test_exit_both_require_reconciliation(tmp_path):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    shell = store.accept(UserRequest("effect", str(tmp_path)), submission_id="shell")
    assert store.claim("shell")
    action_id = store.start_action(shell.task_id, "shell", "shell_exec", {"command": "write && fail"}, str(tmp_path), "execute")
    store.finish_action(action_id, success=False, error_code="nonzero_exit")
    assert store.unknown_actions(shell.task_id)[0]["state"] == "uncertain"
    with pytest.raises(RuntimeError, match="unknown outcome"):
        store.start_action(shell.task_id, "shell", "patch", {"path": "other.py"}, str(tmp_path), "write")

    test = store.accept(UserRequest("test", str(tmp_path)), submission_id="test")
    assert store.claim("test")
    test_id = store.start_action(test.task_id, "test", "run_tests", {"command": "pytest"}, str(tmp_path), "test")
    store.finish_action(test_id, success=False, error_code="nonzero_exit")
    assert store.unknown_actions(test.task_id)[0]["state"] == "uncertain"
    with pytest.raises(RuntimeError, match="unknown outcome"):
        store.start_action(test.task_id, "test", "run_tests", {"command": "pytest", "timeout": 2}, str(tmp_path), "test")


def test_started_action_keeps_redacted_reconciliation_facts_across_v4_upgrade(tmp_path):
    path = tmp_path / "tasks.sqlite3"
    store = TaskStore(path)
    record = store.accept(UserRequest("effect", str(tmp_path)), submission_id="one")
    assert store.claim("one")
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE action_attempts DROP COLUMN arguments_summary")
        connection.execute("PRAGMA user_version=4")
    upgraded = TaskStore(path)
    action_id = upgraded.start_action(record.task_id, "one", "shell_exec",
                                      {"command": "echo ok; api_key=supersecret"}, str(tmp_path), "execute")
    unknown = upgraded.unknown_actions(record.task_id)[0]
    assert unknown["action_id"] == action_id
    assert len(unknown["action_fingerprint"]) == 64
    assert "echo ok" in unknown["arguments_summary"]["command"]
    assert "supersecret" not in str(unknown)
    assert "[REDACTED]" in str(unknown)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5


def test_admission_log_failure_leaves_durable_reconciliation_summary(tmp_path, monkeypatch):
    calls = []

    def effect(_action, _context):
        calls.append("called")
        return ToolResult("effect", True, "done")

    class Model:
        def respond(self, _session):
            return ModelReply("run", actions=[ToolAction("effect", {"command": "api_key=supersecret"})], done=True)

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    record = logger.record

    def failing_record(name, payload):
        if name == "action.admitted":
            raise OSError("log unavailable")
        return record(name, payload)

    monkeypatch.setattr(logger, "record", failing_record)
    tools = ToolRegistry(logger)
    tools.register(SimpleTool("effect", "effect", {"command": "command"}, effect,
                              input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
                              risk_level="execute"))
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode="auto"), logger), logger,
                             approval_callback=lambda *_args: True)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, logger=logger, task_store=store)

    with pytest.raises(OSError, match="log unavailable"):
        runtime.execute(UserRequest("effect", str(tmp_path)), submission_id="run")

    assert calls == []
    task_id = store.get_submission("run").task_id
    unknown = store.unknown_actions(task_id)[0]
    assert unknown["state"] == "started"
    assert unknown["arguments_summary"] == {"command": "api_key=[REDACTED]"}
    assert len(unknown["action_fingerprint"]) == 64


def test_cancel_cleanup_failure_preserves_acceptance_and_retries(tmp_path):
    class FlakyTools:
        calls = 0

        def reset_state(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError("close failed")

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    tools = FlakyTools()
    engine = ExecutionEngine(object(), tools, Guardrails(DefaultPolicy(), logger), logger)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, logger=logger, task_store=store)
    task = runtime.accept(UserRequest("effect", str(tmp_path)), submission_id="one")
    context = ExecutionContext(task_id=task.task_id)
    engine._execution_context = context
    runtime._task_contexts[task.task_id] = context
    runtime._active_context = context

    accepted_at = runtime.cancel(runtime.grant_task_control(task.task_id, {"cancel"}))
    assert store.cancellation_accepted(task.task_id) == accepted_at
    assert context.cancellation.is_set()
    assert not context.tools_closed
    assert not engine._run_state.tools_closed
    assert tools.calls == 1

    engine.cancel_current_run()
    assert context.tools_closed
    assert tools.calls == 2
    engine.cancel_current_run()
    assert tools.calls == 2


@pytest.mark.parametrize("action_name", ["shell_exec", "run_tests"])
def test_reconciliation_requires_no_running_submission_and_stable_command_identity(tmp_path, action_name):
    store = TaskStore(tmp_path / "tasks.sqlite3")
    record = store.accept(UserRequest("send", str(tmp_path)), submission_id="one")
    assert store.claim("one")
    action_id = store.start_action(record.task_id, "one", action_name, {"command": "send", "timeout": 1}, str(tmp_path), "execute")
    with pytest.raises(RuntimeError, match="still running"):
        store.resolve_action(action_id, effect_state="not_performed")
    store.finish("one", ExecutionSummary("unknown", [], outcome="runtime_error"))
    store.resolve_action(action_id, effect_state="performed")
    second = store.accept(UserRequest("retry", str(tmp_path)), submission_id="two", task_id=record.task_id)
    assert store.claim(second.submission_id)
    with pytest.raises(RuntimeError, match="confirmed performed"):
        store.start_action(record.task_id, "two", action_name, {"command": "send", "timeout": 2}, str(tmp_path), "execute")


def test_runtime_cancel_records_in_flight_result_without_starting_next_action(tmp_path):
    started, release = Event(), Event()
    calls = []

    def first_action(_action, _context):
        calls.append("first")
        started.set()
        assert release.wait(3)
        return ToolResult("first_write", True, "written")

    def second_action(_action, _context):
        calls.append("second")
        return ToolResult("second_write", True, "written")

    class Model:
        def respond(self, _session):
            return ModelReply("done", actions=[ToolAction("first_write"), ToolAction("second_write")], done=True)

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    tools = ToolRegistry(logger)
    tools.register(SimpleTool("first_write", "write", {}, first_action, risk_level="write"))
    tools.register(SimpleTool("second_write", "write", {}, second_action, risk_level="write"))
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode="auto"), logger), logger)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, logger=logger, task_store=store)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.execute, UserRequest("write", str(tmp_path)), submission_id="run")
        assert started.wait(3)
        task_id = store.get_submission("run").task_id
        control = runtime.grant_task_control(task_id, {"cancel"})
        runtime.cancel(control)
        release.set()
        with pytest.raises(KeyboardInterrupt):
            future.result(timeout=3)

    assert calls == ["first"]
    assert runtime.last_summary.outcome == "interrupted"
    assert [result.output for result in runtime.last_summary.tool_results] == ["written"]
    assert store.unknown_actions(task_id) == ()
    metrics = next(event.payload for event in logger.events if event.name == "run.metrics")
    assert metrics["real_tool_actions"] == 1
    assert metrics["model_attempts"] == 1
    assert metrics["tool_seconds"] >= 0


def test_effect_then_executor_failure_is_not_replayed_on_task_resume(tmp_path):
    effects = []

    def uncertain_write(_action, _context):
        effects.append("external write")
        raise OSError("connection lost after write")

    class Model:
        def respond(self, _session):
            return ModelReply("done", actions=[ToolAction("uncertain_write")], done=True)

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    tools = ToolRegistry(logger)
    tools.register(SimpleTool("uncertain_write", "write", {}, uncertain_write, risk_level="write"))
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode="auto"), logger), logger)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, logger=logger, task_store=store)

    with pytest.raises(OSError, match="connection lost"):
        runtime.execute(UserRequest("write", str(tmp_path), {"task_id": "task"}), submission_id="first")
    assert len(store.unknown_actions("task")) == 1
    admitted = next(event.payload for event in logger.events if event.name == "action.admitted")
    assert admitted["action_id"] == store.unknown_actions("task")[0]["action_id"]
    assert "arguments_ref" in admitted

    resumed = runtime.execute(
        UserRequest("retry", str(tmp_path), {"task_id": "task"}), submission_id="second",
    )
    assert resumed.outcome == "runtime_error"
    assert effects == ["external write"]


def test_runtime_does_not_retry_timed_out_effect_with_changed_arguments(tmp_path):
    calls = []

    def timed_out(action, _context):
        calls.append(action.arguments["timeout"])
        return ToolResult("effect", False, "response was lost", "timeout")

    class Model:
        turns = 0

        def respond(self, _session):
            self.turns += 1
            return ModelReply(
                "retrying", actions=[ToolAction("effect", {"timeout": self.turns})], done=False,
            )

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    tools = ToolRegistry(logger)
    tools.register(SimpleTool(
        "effect", "effect", {"timeout": "seconds"}, timed_out,
        input_schema={"type": "object", "properties": {"timeout": {"type": "number"}}},
        risk_level="execute",
    ))
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode="auto"), logger), logger,
                             approval_callback=lambda *_args: True)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, logger=logger, task_store=store)

    summary = runtime.execute(UserRequest("effect", str(tmp_path)), submission_id="run")

    assert summary.outcome == "runtime_error"
    assert calls == [1]
    task_id = store.get_submission("run").task_id
    assert store.unknown_actions(task_id)[0]["state"] == "uncertain"


def test_cancel_in_tool_started_callback_prevents_tool_call(tmp_path):
    calls = []

    def write(_action, _context):
        calls.append("called")
        return ToolResult("write", True, "done")

    class Model:
        def respond(self, _session):
            return ModelReply("done", actions=[ToolAction("write")], done=True)

    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    tools = ToolRegistry(logger)
    tools.register(SimpleTool("write", "write", {}, write, risk_level="write"))
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode="auto"), logger), logger)
    store = TaskStore(tmp_path / "tasks.sqlite3")
    runtime = Runtime(engine, logger=logger, task_store=store)

    class Reporter:
        def model_started(self):
            return None

        def model_finished(self, _handle):
            pass

        def tool_started(self, _name):
            task_id = store.get_submission("run").task_id
            runtime.cancel(runtime.grant_task_control(task_id, {"cancel"}))
            return object()

        def tool_aborted(self, _handle):
            pass

    runtime.configure_interaction(progress_reporter=Reporter())
    with pytest.raises(KeyboardInterrupt):
        runtime.execute(UserRequest("write", str(tmp_path)), submission_id="run")
    assert calls == []
    assert store.unknown_actions(store.get_submission("run").task_id) == ()


def test_cleanup_continues_after_failure_and_retries_only_unfinished_resources():
    calls = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name = name
            self.fail = fail

        def close(self):
            calls.append(self.name)
            if self.fail:
                self.fail = False
                raise OSError("close failed")

    registry = ToolRegistry(InMemoryLogger())
    failed = Resource("failed", fail=True)
    registry.attach_state("failed", failed)
    registry.attach_state("temporary", Resource("temporary"))
    persistent = Resource("persistent")
    registry.attach_state("persistent", persistent, persistent=True)
    with pytest.raises(ExceptionGroup):
        registry.reset_state()
    assert calls == ["failed", "temporary", "persistent"]
    assert registry.state_for("temporary") is None
    assert registry.state_for("failed") is failed
    registry.reset_state()
    assert calls == ["failed", "temporary", "persistent", "failed"]
    assert registry.state_for("failed") is None
    assert registry.state_for("persistent") is persistent
    registry.reset_state()
    assert calls[-1] == "persistent"
