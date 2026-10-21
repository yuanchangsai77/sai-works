import json
from dataclasses import replace
from pathlib import Path

import pytest

from saiworks.context.packager import ContextPackager
from saiworks.intent import RequestIntent
from saiworks.model.prompt import ModelPromptBuilder
from saiworks.observability.logger import InMemoryLogger
from saiworks.observations import (
    Observation,
    ObservationArchive,
    ObservationSpec,
    observation_for,
)
from saiworks.orchestration.progress import DefaultProgressPolicy, ProgressContext
from saiworks.orchestration.session import SessionContext
from saiworks.tools.base import SimpleTool
from saiworks.tools.builtin_provider import build_builtin_registry
from saiworks.tools.builtins.git_show import tool as git_show_tool
from saiworks.tools.registry import ToolRegistry
from saiworks.types import ToolAction, ToolResult, UserRequest


def archive_fixture(tmp_path):
    logger = InMemoryLogger(str(tmp_path / "runs"))
    logger.start_run(UserRequest("inspect", str(tmp_path)))
    result = ToolResult("inspect", True, "source body")
    record = Observation.create(ToolAction("inspect"), result, str(tmp_path), "session")
    archive = ObservationArchive(logger)
    record = archive.save(record, result.output)
    return logger, archive, record


def test_canonical_archive_precedes_model_output_budget(tmp_path):
    logger = InMemoryLogger(str(tmp_path / "runs"))
    logger.start_run(UserRequest("inspect", str(tmp_path)))
    registry = ToolRegistry(logger, max_output_bytes=32)
    body = "finding\n" * 1000
    registry.register(
        SimpleTool(
            "inspect", "inspect", {}, lambda *_: ToolResult("inspect", True, body)
        )
    )
    result = registry.execute(ToolAction("inspect"), cwd=str(tmp_path))
    record = observation_for(result)
    assert len(result.output.encode()) <= 32
    assert not result.metadata["_observation_body_complete"]
    assert json.loads(Path(record.reference).read_text())["body"] == body


def test_archive_recovery_uses_session_scope_and_verified_body(tmp_path):
    logger, archive, record = archive_fixture(tmp_path)
    assert archive.recover([logger.run_id], "session", [str(tmp_path)]) == [record]
    assert not archive.recover([logger.run_id], "other", [str(tmp_path)])
    assert not archive.recover([logger.run_id], "session", [str(tmp_path / "other")])
    assert not archive.recover(["../escape", None], "session", [str(tmp_path)])
    path = Path(record.reference)
    payload = json.loads(path.read_text())
    payload["body"] = "corrupted"
    path.write_text(json.dumps(payload))
    assert not archive.recover([logger.run_id], "session", [str(tmp_path)])


@pytest.mark.parametrize("entry", ["index.jsonl", "record"])
def test_recovery_rejects_symlinks_outside_run_archive(tmp_path, entry):
    logger, archive, record = archive_fixture(tmp_path)
    path = (
        Path(record.reference)
        if entry == "record"
        else Path(record.reference).parent / entry
    )
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    assert not archive.recover([logger.run_id], "session", [str(tmp_path)])


def test_archive_failure_does_not_advertise_nonexistent_reference(tmp_path):
    logger, archive, record = archive_fixture(tmp_path)
    logger.run_dir = tmp_path / "file"
    logger.run_dir.write_text("not a directory")
    assert archive.save(replace(record, reference=""), "body").reference == ""


def test_file_cache_requires_current_resource_version(tmp_path):
    path = tmp_path / "design.md"
    path.write_text("first finding")
    registry = build_builtin_registry(InMemoryLogger())
    action = ToolAction("read_file", {"path": path.name})
    result = registry.execute(action, cwd=str(tmp_path))
    assert registry.cached_result_valid(action, result, cwd=str(tmp_path))
    path.write_text("second finding")
    assert not registry.cached_result_valid(action, result, cwd=str(tmp_path))


def test_patch_cannot_use_read_lines_clipped_by_tool_output_budget(tmp_path):
    path = tmp_path / "large.txt"
    lines = [f"{index:04d} " + "a" * 95 + "\n" for index in range(1, 501)]
    path.write_text("".join(lines))
    registry = build_builtin_registry(InMemoryLogger())
    read = registry.execute(ToolAction("read_file", {"path": path.name}), cwd=str(tmp_path))
    assert read.metadata["end_line"] < 400
    diff = f"--- a/{path.name}\n+++ b/{path.name}\n@@ -400,0 +401 @@\n+new line\n"
    patched = registry.execute(ToolAction("patch", {"diff": diff}), cwd=str(tmp_path))
    assert patched.error_code == "file_not_read"
    assert path.read_text().splitlines()[399] == lines[399].rstrip("\n")


def test_search_context_lines_clipped_by_tool_budget_are_not_patch_evidence(tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("".join(f"{index:03d} hit " + "x" * 400 + "\n" for index in range(1, 101)))
    registry = build_builtin_registry(InMemoryLogger())
    result = registry.execute(ToolAction("search_text", {
        "query": "hit", "path": path.name, "context_lines": 20,
    }), cwd=str(tmp_path))
    observed = registry._state["read_files"][str(path)]["observations"][-1]["lines"]
    assert result.metadata["truncated"]
    assert result.metadata["count"] < 90
    assert 90 not in observed


def test_patch_requires_lines_visible_in_the_final_model_context(tmp_path):
    from saiworks.model.prompt import ModelPromptBuilder
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    path = tmp_path / "design.md"
    lines = [f"{index:03d} " + "x" * 80 + "\n" for index in range(1, 201)]
    path.write_text("".join(lines))
    diff = f"--- a/{path.name}\n+++ b/{path.name}\n@@ -180,0 +181 @@\n+new line\n"

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            ModelPromptBuilder(ContextPackager(12_000)).build_messages(session)
            if self.calls == 1:
                return ModelReply("read", actions=[ToolAction("read_file", {"path": path.name})])
            if self.calls == 2:
                return ModelReply("patch", actions=[ToolAction("patch", {"diff": diff})])
            return ModelReply("The patch was blocked because the affected line was not visible.", done=True)

    logger = InMemoryLogger()
    runtime = ExecutionEngine(model=Model(), tools=build_builtin_registry(logger), logger=logger,
                              guardrails=Guardrails(DefaultPolicy(mode="auto"), logger))
    summary = runtime.execute(UserRequest("Review and edit the design", str(tmp_path)))
    assert any(result.name == "patch" and result.error_code == "file_not_read"
               for result in summary.tool_results)
    assert path.read_text() == "".join(lines)


@pytest.mark.parametrize("build_prompt", [True, False])
def test_same_turn_read_cannot_authorize_unseen_zero_context_patch(tmp_path, build_prompt):
    from saiworks.model.prompt import ModelPromptBuilder
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    path = tmp_path / "target.txt"
    path.write_text("before\n")
    diff = "--- a/target.txt\n+++ b/target.txt\n@@ -1,0 +2 @@\n+after\n"

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            if build_prompt:
                ModelPromptBuilder(ContextPackager()).build_messages(session)
            if self.calls == 1:
                return ModelReply("read and insert", actions=[
                    ToolAction("read_file", {"path": path.name}),
                    ToolAction("patch", {"diff": diff}),
                ])
            if self.calls == 2:
                return ModelReply("retry after seeing the read", actions=[ToolAction("patch", {"diff": diff})])
            return ModelReply("done", done=True)

    logger = InMemoryLogger()
    model = Model()
    runtime = ExecutionEngine(model=model, tools=build_builtin_registry(logger), logger=logger,
                              guardrails=Guardrails(DefaultPolicy(mode="auto"), logger))
    summary = runtime.execute(UserRequest("Insert after the first line", str(tmp_path)))

    patches = [result for result in summary.tool_results if result.name == "patch"]
    assert [result.error_code for result in patches] == ["file_not_read", None]
    assert model.calls == 3
    assert path.read_text() == "before\nafter\n"


def test_repeated_read_with_same_observation_id_cannot_expand_partial_model_view(tmp_path):
    from saiworks.model.prompt import ModelPromptBuilder
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    path = tmp_path / "target.txt"
    lines = [f"{index:03d} " + "x" * 80 + "\n" for index in range(1, 201)]
    path.write_text("".join(lines))
    diff = "--- a/target.txt\n+++ b/target.txt\n@@ -180,0 +181 @@\n+new line\n"

    class Model:
        calls = 0

        def respond(self, session):
            self.calls += 1
            ModelPromptBuilder(ContextPackager(12_000)).build_messages(session)
            if self.calls == 1:
                return ModelReply("read", actions=[ToolAction("read_file", {"path": path.name})])
            if self.calls == 2:
                assert session.visible_observation_views
                assert all(not view["complete"] for view in session.visible_observation_views)
                return ModelReply("read again and insert", actions=[
                    ToolAction("read_file", {"path": path.name}),
                    ToolAction("patch", {"diff": diff}),
                ])
            return ModelReply("done", done=True)

    logger = InMemoryLogger()
    runtime = ExecutionEngine(model=Model(), tools=build_builtin_registry(logger), logger=logger,
                              guardrails=Guardrails(DefaultPolicy(mode="auto"), logger))
    summary = runtime.execute(UserRequest("Insert after line 180", str(tmp_path)))

    reads = [result for result in summary.tool_results if result.name == "read_file"]
    patches = [result for result in summary.tool_results if result.name == "patch"]
    assert len(reads) == 2
    assert observation_for(reads[0]).identifier == observation_for(reads[1]).identifier
    assert patches[0].error_code == "file_not_read"
    assert path.read_text() == "".join(lines)


def test_search_at_match_limit_is_not_a_complete_observation(tmp_path):
    (tmp_path / "many.txt").write_text("hit\n" * 201)
    registry = build_builtin_registry(InMemoryLogger())
    result = registry.execute(
        ToolAction("search_text", {"query": "hit", "path": "many.txt"}),
        cwd=str(tmp_path),
    )
    assert result.metadata["count"] == 200
    assert result.metadata["truncated"] is True
    assert observation_for(result).complete is False


def test_search_clipped_before_match_limit_is_not_a_complete_observation(tmp_path):
    (tmp_path / "many.txt").write_text(("hit " + "x" * 200 + "\n") * 100)
    registry = ToolRegistry(InMemoryLogger(), max_output_bytes=2_000)
    from saiworks.tools.builtins.search_text import tool as search_text_tool

    registry.register(search_text_tool())
    result = registry.execute(
        ToolAction("search_text", {"query": "hit", "path": "many.txt"}),
        cwd=str(tmp_path),
    )
    assert result.metadata["count"] < 200
    assert all(match["line"] is not None for match in result.metadata["matches"])
    assert observation_for(result).complete is False


def test_long_reference_is_atomic_and_actual_visibility_is_recorded(tmp_path):
    session = SessionContext(UserRequest("review", str(tmp_path)))
    body = "finding\n" * 2000
    result = ToolResult("inspect", True, body)
    record = Observation.create(
        ToolAction("inspect"), result, str(tmp_path), spec=ObservationSpec(certain=True)
    )
    record = replace(record, reference="/runs/" + "segment/" * 110 + "record.json")
    result.metadata["_observation"] = record.payload()
    session.add_tool_result(result)
    builder = ModelPromptBuilder(ContextPackager(12_000))
    messages = builder.build_messages(session)
    text = json.dumps(messages)
    assert record.reference in text
    assert "_observation" not in text and "_complete" not in text
    assert record.identifier not in session.visible_observations
    assert not builder.context_packager.last_views[-1]["complete"]
    ModelPromptBuilder().build_messages(session)
    assert record.identifier in session.visible_observations
    assert session.observations[0] == record


def test_progress_requires_known_complete_visible_prior_observations(tmp_path):
    result = ToolResult("inspect", True, "same")
    record = Observation.create(
        ToolAction("inspect"), result, str(tmp_path), spec=ObservationSpec(certain=True)
    )
    result.metadata["_observation"] = record.payload()
    policy = DefaultProgressPolicy()

    def evaluate(visible):
        return policy.evaluate(
            ProgressContext(
                RequestIntent(True, False), [result], False, [result, result], visible
            )
        )

    assert evaluate(frozenset()) is None
    assert evaluate(frozenset([record.identifier])) is not None
    result.metadata["_observation"] = replace(record, certain=False).payload()
    assert evaluate(None) is None


def test_distinct_git_revisions_with_identical_body_are_distinct_observations(
    tmp_path, monkeypatch
):
    from saiworks.tools.builtins import git_show

    monkeypatch.setattr(git_show, "ensure_git_repository", lambda *_: None)
    monkeypatch.setattr(
        git_show,
        "run_command",
        lambda *_args, **_kwargs: ToolResult(
            "command", True, "same", metadata={"stdout": "same"}
        ),
    )
    registry = ToolRegistry(InMemoryLogger())
    registry.register(git_show_tool())
    results = [
        registry.execute(
            ToolAction("git_show", {"revision": f"HEAD~{index}:AGENTS.md"}),
            cwd=str(tmp_path),
        )
        for index in range(4)
    ]
    assert len({observation_for(result).evidence_key for result in results}) == 4
    assert (
        DefaultProgressPolicy().evaluate(
            ProgressContext(
                RequestIntent(True, False), results[-1:], False, results[:-1]
            )
        )
        is None
    )


@pytest.mark.parametrize("payload", [None, {}, {"identifier": []}])
def test_malformed_observation_is_not_trusted(payload):
    assert Observation.from_payload(payload) is None


def test_partial_model_view_allows_repeated_read_until_budget(tmp_path):
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    (tmp_path / "source.md").write_text("observed design\n" * 3000)

    class Model:
        calls = 0

        def respond(self, session):
            ModelPromptBuilder(ContextPackager(12_000)).build_messages(session)
            self.calls += 1
            return ModelReply(
                "read missing details",
                actions=[ToolAction("read_file", {"path": "source.md"})],
            )

    logger = InMemoryLogger(str(tmp_path / "runs"))
    model = Model()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        logger=logger,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        max_turns=8,
    )
    summary = engine.execute(UserRequest("review", str(tmp_path)))
    assert model.calls == 8
    assert summary.outcome == "exhausted"
    assert not any(
        result.error_code in {"duplicate_tool_call", "progress_required"}
        for result in summary.tool_results
    )
    assert all(not result.metadata.get("duplicate") for result in summary.tool_results)


def test_unknown_read_semantics_use_budget_instead_of_hard_stall(tmp_path):
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    class Model:
        def respond(self, _session):
            return ModelReply("inspect", actions=[ToolAction("external")])

    logger = InMemoryLogger(str(tmp_path / "runs"))
    registry = ToolRegistry(logger)
    registry.register(
        SimpleTool(
            "external", "external", {}, lambda *_: ToolResult("external", True, "same")
        )
    )
    engine = ExecutionEngine(
        model=Model(),
        tools=registry,
        logger=logger,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
        max_turns=8,
    )
    summary = engine.execute(UserRequest("review", str(tmp_path)))
    assert summary.outcome == "exhausted"
    assert all(result.success for result in summary.tool_results)


def test_structured_recovery_preserves_missing_trace_details(tmp_path):
    logger, archive, record = archive_fixture(tmp_path)
    session = SessionContext(
        UserRequest(
            "resume",
            str(tmp_path),
            metadata={
                "session_trace": [
                    {
                        "run_id": logger.run_id,
                        "turns": [{"tool_result_details": [
                            "inspect [ok] source body", "inspect [ok] MISSING_FINDING",
                        ]}],
                    }
                ]
            },
        )
    )
    session.recovered_observations = archive.recover(
        [logger.run_id], "session", [str(tmp_path)]
    )
    messages = ModelPromptBuilder().build_messages(session)
    text = "\n".join(str(message.get("content", "")) for message in messages)
    assert record.reference in text and "source body" in text
    assert "MISSING_FINDING" in text
    assert "Prior observations (legacy partial summaries, not complete evidence):\ninspect [ok] source body" not in text
    assert record.identifier not in session.visible_observations


def test_recovery_keeps_same_output_from_a_different_request(tmp_path):
    logger = InMemoryLogger(str(tmp_path / "runs"))
    logger.start_run(UserRequest("inspect", str(tmp_path)))
    action = ToolAction("read_file", {"path": "first.md"})
    result = ToolResult("read_file", True, "same finding")
    record = ObservationArchive(logger).save(
        Observation.create(action, result, str(tmp_path), "session"), result.output,
    )
    session = SessionContext(UserRequest("resume", str(tmp_path), metadata={
        "session_trace": [{"run_id": logger.run_id, "turns": [{"tool_result_details": [
            'read_file [ok] {"path": "first.md"} same finding',
            'read_file [ok] {"path": "second.md"} same finding',
        ]}]}],
    }))
    session.recovered_observations = [record]
    messages = ModelPromptBuilder().build_messages(session)
    text = "\n".join(str(message.get("content", "")) for message in messages)
    assert 'Prior observations (legacy partial summaries, not complete evidence):\nread_file [ok] {"path": "second.md"} same finding' in text
    assert 'Prior observations (legacy partial summaries, not complete evidence):\nread_file [ok] {"path": "first.md"} same finding' not in text


def test_archived_body_and_arguments_are_redacted(tmp_path):
    logger, archive, _ = archive_fixture(tmp_path)
    result = ToolResult("inspect", True, "api_key=private-value")
    action = ToolAction("inspect", {"api_key": "private-value"})
    record = Observation.create(action, result, str(tmp_path), "session")
    record = archive.save(record, result.output, action.arguments)
    stored = Path(record.reference).read_text()
    assert "private-value" not in stored
    assert "[REDACTED]" in stored
    assert record in archive.recover([logger.run_id], "session", [str(tmp_path)])


def test_non_utf8_recovery_index_is_ignored(tmp_path):
    logger, archive, record = archive_fixture(tmp_path)
    (Path(record.reference).parent / "index.jsonl").write_bytes(b"\xff")
    assert not archive.recover([logger.run_id], "session", [str(tmp_path)])


@pytest.mark.parametrize("protocol", ["native_tools", "prompt_json"])
@pytest.mark.parametrize("read_first", [True, False])
def test_new_external_records_are_not_blocked_by_cached_read(
    tmp_path, protocol, read_first
):
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    (tmp_path / "config.md").write_text("unchanged configuration")

    class Model:
        calls = 0

        def respond(self, session):
            ModelPromptBuilder().build_messages(session)
            self.calls += 1
            if self.calls == 9:
                return ModelReply("Collected eight new records.", done=True)
            actions = [
                ToolAction("read_file", {"path": "config.md"}),
                ToolAction("external", {"page": self.calls}),
            ]
            return ModelReply(
                "collect", actions=actions if read_first else actions[::-1]
            )

    logger = InMemoryLogger(str(tmp_path / "runs"))
    registry = build_builtin_registry(logger)
    registry.register(
        SimpleTool(
            "external",
            "external",
            {"page": "page"},
            lambda action, _context: ToolResult(
                "external", True, f"new record {action.arguments['page']}"
            ),
        )
    )
    model = Model()
    engine = ExecutionEngine(
        model=model,
        tools=registry,
        logger=logger,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
    )
    summary = engine.execute(
        UserRequest(
            "Collect all records",
            str(tmp_path),
            metadata={
                "model_capability_profile": {"action_protocol": protocol},
            },
        )
    )
    assert summary.outcome == "completed"
    assert model.calls == 9
    assert all(result.success for result in summary.tool_results)
    reads = [result for result in summary.tool_results if result.name == "read_file"]
    assert len(reads) == 8
    assert all(result.metadata.get("duplicate") for result in reads[1:])


def test_text_protocol_messages_only_emit_role_and_content(tmp_path):
    session = SessionContext(
        UserRequest(
            "inspect",
            str(tmp_path),
            metadata={
                "model_capability_profile": {"action_protocol": "prompt_json"},
            },
        )
    )
    session.add_model_message("read", [ToolAction("read_file", {"path": "config.md"})])
    session.add_tool_result(ToolResult("read_file", True, "configuration"))
    messages = ModelPromptBuilder().build_messages(session)
    assert all(set(message) == {"role", "content"} for message in messages)
    assert "configuration" in json.dumps(messages)
    assert session.visible_observations


def test_cached_read_only_loop_still_recovers_then_stops(tmp_path):
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.types import ModelReply

    (tmp_path / "config.md").write_text("unchanged configuration")

    class Model:
        calls = 0

        def respond(self, session):
            ModelPromptBuilder().build_messages(session)
            self.calls += 1
            return ModelReply(
                "inspect", actions=[ToolAction("read_file", {"path": "config.md"})]
            )

    logger = InMemoryLogger(str(tmp_path / "runs"))
    model = Model()
    engine = ExecutionEngine(
        model=model,
        tools=build_builtin_registry(logger),
        logger=logger,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
    )
    summary = engine.execute(UserRequest("review", str(tmp_path)))
    assert model.calls == 4
    assert summary.outcome == "stalled"
    assert (
        sum(result.error_code == "progress_required" for result in summary.tool_results)
        == 1
    )
    assert not any(
        result.error_code == "duplicate_tool_call" for result in summary.tool_results
    )
