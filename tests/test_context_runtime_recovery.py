import pytest

from saiworks.model.types import ModelTimeoutError
from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.tools.base import SimpleTool
from saiworks.tools.builtin_provider import build_builtin_registry
from saiworks.types import ModelReply, ToolAction, ToolResult, UserRequest


def engine(model, logger, **kwargs):
    return ExecutionEngine(model=model, logger=logger, tools=build_builtin_registry(logger),
                           guardrails=Guardrails(DefaultPolicy(mode="auto"), logger), **kwargs)


@pytest.mark.parametrize("recover", [True, False])
def test_analysis_loop_with_varied_read_parameters_gets_recovery_then_stops_or_finishes(tmp_path, recover):
    (tmp_path / "design.md").write_text("one design finding\n")

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            if recover and any(result.error_code == "progress_required" for result in session.tool_results):
                return ModelReply("Use the existing design finding to finish the review.", done=True)
            return ModelReply("inspect", actions=[ToolAction("read_file", {
                "path": "design.md", "start_line": 1, "end_line": self.calls + 1,
            })])

    logger = InMemoryLogger()
    model = Model()
    summary = engine(model, logger).execute(UserRequest("只分析设计，不修改文件", str(tmp_path)))
    assert model.calls == 4
    assert summary.outcome == ("completed" if recover else "stalled")
    assert any(result.error_code == "progress_required" for result in summary.tool_results)
    assert (tmp_path / "design.md").read_text() == "one design finding\n"


def test_successful_execute_does_not_erase_semantic_read_loop_detection(tmp_path):
    (tmp_path / "design.md").write_text("same finding\n")

    class Model:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            return ModelReply("inspect", actions=[
                ToolAction("noop_execute", {"attempt": self.calls}),
                ToolAction("read_file", {"path": "design.md"}),
            ])

    logger = InMemoryLogger()
    model = Model()
    runtime = engine(model, logger)
    runtime.tools.register(SimpleTool(name="noop_execute", description="Test execution.", arguments={"attempt": "iteration"},
                                      risk_level="execute", handler=lambda _action, _context: ToolResult("noop_execute", True, "ok")))
    summary = runtime.execute(UserRequest("review", str(tmp_path)))
    assert model.calls == 4
    assert summary.outcome == "stalled"
    assert any(result.error_code == "progress_required" for result in summary.tool_results)


def test_approval_wait_does_not_consume_execution_budget_and_resets_per_run(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("saiworks.orchestration.engine.time.monotonic", lambda: clock[0])

    class Model:
        def respond(self, session):
            if not session.tool_results:
                return ModelReply("execute", actions=[ToolAction("noop_execute")])
            return ModelReply("done", done=True)

    def approve(_action, _reason):
        clock[0] += 1_200
        return True

    logger = InMemoryLogger()
    runtime = engine(Model(), logger, max_run_seconds=2, approval_callback=approve)
    runtime.guardrails = Guardrails(DefaultPolicy(mode="confirm"), logger)
    runtime.tools.register(SimpleTool(name="noop_execute", description="Test execution.", arguments={},
                                      risk_level="execute", handler=lambda _action, _context: ToolResult("noop_execute", True, "ok")))
    for _ in range(2):
        summary = runtime.execute(UserRequest("execute", str(tmp_path)))
        assert summary.outcome == "completed"
        assert runtime._execution_context.approval_wait_seconds == 1_200
    approvals = [event for event in logger.events if event.name == "safety.approval"]
    assert all(event.payload["wait_seconds"] == 1_200 for event in approvals)


def test_transport_timeout_still_consumes_execution_budget(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("saiworks.orchestration.engine.time.monotonic", lambda: clock[0])

    class Model:
        def respond(self, _session):
            clock[0] += 3
            raise ModelTimeoutError("request timed out")

    logger = InMemoryLogger()
    summary = engine(Model(), logger, max_run_seconds=2).execute(UserRequest("review", str(tmp_path)))
    assert summary.outcome == "exhausted"
    assert summary.blockers[0].error_code == "run_time_budget_exhausted"
    assert summary.checkpoint.runtime_state["model_attempts"] == "1"


def test_default_run_has_no_wall_clock_limit(tmp_path, monkeypatch):
    (tmp_path / "design.md").write_text("finding\n")
    clock = [0.0]
    monkeypatch.setattr("saiworks.orchestration.engine.time.monotonic", lambda: clock[0])

    class Model:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 1:
                clock[0] += 1_200
                return ModelReply("inspect", actions=[ToolAction("read_file", {"path": "design.md"})])
            return ModelReply("done", done=True)

    logger = InMemoryLogger()
    model = Model()
    summary = engine(model, logger).execute(UserRequest("review", str(tmp_path)))
    assert model.calls == 2
    assert summary.outcome == "completed"


def test_context_summary_requests_share_existing_model_attempt_budget(tmp_path, monkeypatch):
    from saiworks.context import ExplicitContextItem
    from saiworks.model.client import OpenAICompatibleModelClient
    from saiworks.tools.registry import ToolRegistry

    class LargeContext:
        def load_context(self, _request, session):
            session.explicit_context = [ExplicitContextItem("large.txt", "large.txt", "file", "X" * 100_000)]

    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000", logger=logger, context_budget_chars=12_000,
    )
    requests = []

    def respond(_url, payload, **_kwargs):
        requests.append(payload)
        return {"choices": [{"message": {"content": "summary"}}]}

    monkeypatch.setattr(client, "_post_json", respond)
    runtime = ExecutionEngine(
        model=client, logger=logger, tools=ToolRegistry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        context_loaders=[LargeContext()], max_model_attempts=3,
    )
    summary = runtime.execute(UserRequest("review", str(tmp_path)))
    assert summary.outcome == "exhausted"
    assert summary.blockers[0].error_code == "model_attempt_budget_exhausted"
    assert summary.checkpoint.runtime_state["model_attempts"] == "3"
    assert len(requests) == 3
    assert sum(event.name == "context.compression_request_complete" for event in logger.events) == 3


def test_successful_context_summary_counts_primary_request_once(tmp_path, monkeypatch):
    from saiworks.context import ExplicitContextItem
    from saiworks.model.client import OpenAICompatibleModelClient
    from saiworks.tools.registry import ToolRegistry

    class LargeContext:
        def load_context(self, _request, session):
            session.explicit_context = [ExplicitContextItem("large.txt", "large.txt", "file", "X" * 20_000)]

    logger = InMemoryLogger()
    client = OpenAICompatibleModelClient(
        base_url="http://127.0.0.1:3000", logger=logger, context_budget_chars=12_000,
    )
    requests = []

    def respond(_url, payload, **_kwargs):
        requests.append(payload)
        content = ("summary" if "Summarize optional runtime context" in payload["messages"][0]["content"]
                   else '{"message":"done","done":true,"actions":[]}')
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(client, "_post_json", respond)
    runtime = ExecutionEngine(
        model=client, logger=logger, tools=ToolRegistry(logger),
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        context_loaders=[LargeContext()], max_model_attempts=10,
    )
    summary = runtime.execute(UserRequest("review", str(tmp_path)))
    assert summary.outcome == "completed"
    assert int(summary.checkpoint.runtime_state["model_attempts"]) == len(requests)
    assert sum(event.name == "model.request" for event in logger.events) == 1


@pytest.mark.parametrize("tool,argument,conditions", [
    ("find_files", "pattern", ["*.tsx", "*.vue", "*.svelte", "*.qml"]),
    ("search_text", "query", ["React", "Vue", "Svelte", "Qt"]),
])
def test_distinct_negative_searches_complete_without_false_progress_recovery(tmp_path, tool, argument, conditions):
    (tmp_path / "README.md").write_text("unrelated text\n")

    class Model:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls > len(conditions):
                return ModelReply("Completed the four independent searches.", done=True)
            return ModelReply("check another condition", actions=[
                ToolAction(tool, {argument: conditions[self.calls - 1]}),
            ])

    logger = InMemoryLogger()
    model = Model()
    summary = engine(model, logger).execute(UserRequest("Identify the frontend framework.", str(tmp_path)))
    assert summary.outcome == "completed"
    assert model.calls == 5
    assert not any(result.error_code == "progress_required" for result in summary.tool_results)


def test_new_extension_results_keep_progress_despite_repeated_config_reads(tmp_path):
    (tmp_path / "config.txt").write_text("shared interpretation rules\n")

    class Model:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 5:
                return ModelReply("Collected four distinct records.", done=True)
            return ModelReply("collect next record", actions=[
                ToolAction("lookup_record", {"id": self.calls}),
                ToolAction("read_file", {"path": "config.txt"}),
            ])

    logger = InMemoryLogger()
    model = Model()
    runtime = engine(model, logger)
    runtime.tools.register(SimpleTool(
        name="lookup_record", description="Retrieve a distinct record.", arguments={"id": "Record id"},
        handler=lambda action, _context: ToolResult("lookup_record", True, f"new record {action.arguments['id']}"),
    ))
    summary = runtime.execute(UserRequest("Interpret four independent records using shared rules.", str(tmp_path)))
    assert summary.outcome == "completed"
    assert model.calls == 5
    assert not any(result.error_code == "progress_required" for result in summary.tool_results)


def test_equal_negative_searches_in_distinct_workspaces_are_independent(tmp_path):
    directories = [tmp_path / str(index) for index in range(4)]
    for directory in directories:
        directory.mkdir()
        (directory / "README.md").write_text("no frontend here\n")

    class Model:
        calls = 0

        def respond(self, _session):
            self.calls += 1
            if self.calls == 5:
                return ModelReply("Finished checking all four workspaces.", done=True)
            return ModelReply("inspect next workspace", actions=[
                ToolAction("workspace_open", {"path": str(directories[self.calls - 1])}),
                ToolAction("find_files", {"pattern": "*.tsx"}),
            ])

    logger = InMemoryLogger()
    model = Model()
    runtime = engine(model, logger)
    summary = runtime.execute(UserRequest("Check each of the four directories for frontend files.", str(tmp_path)))
    assert summary.outcome == "completed"
    assert model.calls == 5
    assert not any(result.error_code == "progress_required" for result in summary.tool_results)
