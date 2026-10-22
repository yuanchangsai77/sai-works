import copy
import json

import pytest

from saiworks.context.packager import (
    ContextBudgetExceededError,
    ContextPackager,
    ContextSegment,
    ContextSummaryTooLongError,
)
from saiworks.model.prompt import ModelPromptBuilder
from saiworks.observations import Observation, ObservationSpec
from saiworks.orchestration.session import SessionContext
from saiworks.types import ToolAction, ToolDefinition, ToolResult, UserRequest


def make_session(protocol="native_tools", **metadata):
    metadata["model_capability_profile"] = {"action_protocol": protocol}
    return SessionContext(request=UserRequest(prompt="先改设计稿，定稿后再修改实现", cwd="/repo", metadata=metadata))


def read(session, path, output, **metadata):
    action = ToolAction("read_file", {"path": path})
    session.add_model_message("inspect relevant source", [action])
    result = ToolResult("read_file", True, output, metadata={"action_arguments": action.arguments, **metadata})
    result.metadata["_observation"] = Observation.create(action, result, "/repo", spec=ObservationSpec(
        target="/repo/" + path, coverage=(metadata.get("start_line", 1), metadata.get("end_line", output.count("\n") + 1)),
        certain=True, complete=not metadata.get("truncated", False))).payload()
    session.add_tool_result(result)


def build(session, budget=120_000):
    builder = ModelPromptBuilder(ContextPackager(budget))
    messages = builder.build_messages(session)
    return builder, messages, "\n".join(str(message.get("content") or "") for message in messages)


def assert_paired(messages):
    pending = []
    for message in messages:
        if message.get("tool_calls"):
            assert not pending
            pending = [call["id"] for call in message["tool_calls"]]
        elif message["role"] == "tool":
            assert message["tool_call_id"] == pending.pop(0)
        else:
            assert not pending
    assert not pending


@pytest.mark.parametrize("protocol", ["native_tools", "prompt_json"])
def test_full_latest_read_uses_spare_budget(protocol):
    session = make_session(protocol)
    output = "\n".join(f"line {index}: observed design" for index in range(800))
    read(session, "design.md", output)
    builder, messages, text = build(session)
    assert output in text
    assert builder.context_packager.last_stats.truncated_observations == 0
    if protocol == "native_tools":
        assert any(message["role"] == "tool" for message in messages)
        assert_paired(messages)
    else:
        assert all(message["role"] in {"system", "user", "assistant"} for message in messages)


def test_optional_trailing_context_cannot_make_required_request_over_budget():
    packager = ContextPackager(4_000)
    system = [ContextSegment("s" * 3_900, "system", required=True)]
    current = [ContextSegment("u" * 100, "request", required=True)]

    messages = packager.package_segments(
        system, [], current, [ContextSegment("optional", "recovery")],
    )

    assert messages == [
        {"role": "system", "content": "s" * 3_900},
        {"role": "user", "content": "u" * 100},
    ]
    assert packager.last_stats.included_chars == 4_000
    assert packager.last_stats.truncated_current is True


def test_optional_context_uses_model_summary_when_budget_is_tight():
    packager = ContextPackager(4_000)
    calls = []

    def compress(segment, limit):
        calls.append((segment.label, limit))
        return "AI SUMMARY: keep the relevant fact"

    messages = packager.package_segments(
        [ContextSegment("S" * 1_000, "required", required=True),
         ContextSegment("O" * 100, "optional")],
        [], [ContextSegment("U" * 2_960, "request", required=True)],
        compress_segment=compress,
    )

    assert calls == [("optional", 38)]
    assert "AI SUMMARY: keep the relevant fact" in messages[0]["content"]
    assert "Runtime omitted lower-priority context" not in messages[0]["content"]
    assert sum(len(message["content"]) for message in messages) <= 4_000


def test_overflow_summarizes_older_observations_and_dialogue_together():
    packager = ContextPackager(4_000)
    old = Observation.create(
        ToolAction("read_file", {"path": "old.txt"}),
        ToolResult("read_file", True, "older observation"), "/repo",
        spec=ObservationSpec(target="/repo/old.txt", certain=True),
    )
    newest = Observation.create(
        ToolAction("read_file", {"path": "new.txt"}),
        ToolResult("read_file", True, "latest observation"), "/repo",
        spec=ObservationSpec(target="/repo/new.txt", certain=True),
    )
    history = [
        {"role": "user", "content": "older question " + "Q" * 500},
        {"role": "assistant", "content": "older answer " + "A" * 500},
        {"role": "user", "content": "latest question"},
        {"role": "assistant", "content": "latest answer"},
    ]
    calls = []

    def compress(segment, limit):
        calls.append((segment.content, limit))
        return "combined earlier findings"

    messages = packager.package_segments(
        [ContextSegment("rules", "rules", required=True)], history,
        [ContextSegment("request", "request", required=True)],
        observation_groups=[
            [old.message("older observation " + "O" * 3_000, complete=True)],
            [
                {"role": "assistant", "content": "inspect latest source", "tool_calls": [{
                    "id": "call-new", "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"new.txt"}'},
                }]},
                newest.message("latest observation", role="tool", complete=True,
                               tool_call_id="call-new"),
            ],
        ],
        compress_segment=compress,
    )
    rendered = "\n".join(str(message.get("content", "")) for message in messages)
    assert len(calls) == 1
    assert "older question" in calls[0][0] and "older observation" in calls[0][0]
    assert "combined earlier findings" in rendered
    assert "latest observation" in rendered and "latest question" in rendered
    assert "O" * 3_000 not in rendered
    assert_paired(messages)
    assert packager.last_visibility == frozenset({newest.identifier})
    assert packager.last_stats.included_chars <= 4_000


def test_oversized_latest_observation_is_summarized_without_read_visibility():
    packager = ContextPackager(4_000)
    record = Observation.create(
        ToolAction("read_file", {"path": "large.txt"}),
        ToolResult("read_file", True, "large observation"), "/repo",
        spec=ObservationSpec(target="/repo/large.txt", certain=True),
    )
    calls = []
    messages = packager.package_segments(
        [ContextSegment("rules", "rules", required=True)], [],
        [ContextSegment("request", "request", required=True)],
        observation_groups=[[record.message("X" * 6_000, complete=True)]],
        compress_segment=lambda segment, limit: (calls.append((segment, limit)) or "large source summary"),
    )
    assert len(calls) == 1
    assert "X" * 6_000 in calls[0][0].content
    assert any("large source summary" in str(message.get("content")) for message in messages)
    assert packager.last_visibility == frozenset()
    assert packager.last_stats.included_chars <= 4_000


def test_summary_replans_when_preserved_observation_leaves_too_little_room():
    packager = ContextPackager(4_000)
    calls = []

    def compress(segment, limit):
        calls.append((segment.content, limit))
        if limit < 200:
            raise ContextSummaryTooLongError("summary needs more space")
        return "earlier work and latest observation summarized"

    messages = packager.package_segments(
        [ContextSegment("rules", "rules", required=True)],
        [{"role": "user", "content": "old question " + "Q" * 100},
         {"role": "assistant", "content": "old answer " + "A" * 100}],
        [ContextSegment("request", "request", required=True)],
        observation_groups=[[{"role": "user", "content": "Current-run observation\n" + "X" * 3_800}]],
        compress_segment=compress,
    )
    assert len(calls) == 2
    assert calls[1][1] > calls[0][1]
    assert "X" * 3_800 in calls[1][0]
    assert any("latest observation summarized" in str(message.get("content")) for message in messages)
    assert packager.last_stats.included_chars <= 4_000


def test_small_remaining_budget_can_hold_a_brief_work_summary():
    packager = ContextPackager(4_000)
    calls = []
    messages = packager.package_segments(
        [ContextSegment("R" * 3_890, "rules", required=True)], [],
        [ContextSegment("request", "request", required=True)],
        observation_groups=[[{"role": "user", "content": "Current-run observation\n" + "X" * 500}]],
        compress_segment=lambda segment, limit: (calls.append((segment, limit)) or "brief finding"),
    )
    assert len(calls) == 1
    assert any("brief finding" in str(message.get("content")) for message in messages)
    assert packager.last_stats.included_chars <= 4_000


def test_optional_context_is_not_compressed_when_complete_request_fits():
    packager = ContextPackager(120_000)
    calls = []
    messages = packager.package_segments(
        [ContextSegment("R" * 1_000, "rules", required=True),
         ContextSegment("W" * 70_000, "workspace")],
        [], [ContextSegment("Q" * 100, "request", required=True)],
        compress_segment=lambda segment, limit: (calls.append((segment.label, limit)) or "summary"),
    )
    assert calls == []
    assert "W" * 70_000 in messages[0]["content"]
    assert packager.last_stats.included_chars < 120_000


def test_complete_request_keeps_trailing_history_and_observation_without_compression():
    packager = ContextPackager(4_000)
    calls = []
    conversation = [{"role": "user", "content": "old question" * 20},
                    {"role": "assistant", "content": "old answer" * 20}]
    messages = packager.package_segments(
        [ContextSegment("R" * 500, "rules", required=True),
         ContextSegment("W" * 700, "workspace")],
        conversation, [ContextSegment("Q" * 100, "request", required=True)],
        [ContextSegment("old recovery" * 30, "recovery")],
        observation_groups=[[{"role": "user", "content": "Observation: {}\n" + "O" * 600}]],
        compress_segment=lambda segment, limit: (calls.append((segment.label, limit)) or "summary"),
    )
    assert calls == []
    assert all(item in messages for item in conversation)
    assert any("O" * 600 in str(item["content"]) for item in messages)
    assert any("old recovery" * 30 in str(item["content"]) for item in messages)
    assert packager.last_stats.truncated_system is False
    assert packager.last_stats.truncated_current is False


def test_pending_tool_call_does_not_trigger_context_compression():
    packager = ContextPackager(4_000)
    calls = []
    pending = [[{"role": "assistant", "content": "pending" * 450, "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
    ]}]]
    messages = packager.package_segments(
        [ContextSegment("R" * 500, "rules", required=True),
         ContextSegment("W" * 1_000, "workspace")],
        [], [ContextSegment("Q" * 100, "request", required=True)],
        observation_groups=pending,
        compress_segment=lambda segment, limit: (calls.append((segment.label, limit)) or "summary"),
    )
    assert calls == []
    assert "W" * 1_000 in messages[0]["content"]
    assert packager.last_stats.omitted_observations == 1
    assert packager.last_stats.included_chars < 4_000


def test_model_summary_that_does_not_fit_is_rejected_without_truncation():
    packager = ContextPackager(4_000)
    with pytest.raises(ContextBudgetExceededError, match="compression did not fit"):
        packager.package_segments(
            [ContextSegment("S" * 1_000, "required", required=True),
             ContextSegment("O" * 100, "optional")],
            [], [ContextSegment("U" * 2_960, "request", required=True)],
            compress_segment=lambda _segment, _limit: "uncompressed" * 10,
        )


def test_local_fallback_never_exceeds_budget_when_marker_does_not_fit():
    packager = ContextPackager(4_000)
    messages = packager.package_segments(
        [ContextSegment("S" * 1_000, "required", required=True),
         ContextSegment("O" * 100, "optional")],
        [], [ContextSegment("U" * 2_960, "request", required=True)],
    )
    assert sum(len(message["content"]) for message in messages) <= 4_000


def test_small_finding_between_large_reads_survives_when_total_fits():
    session = make_session()
    read(session, "old.md", "old line\n" * 2_000)
    read(session, "decision.md", "UNIQUE_DESIGN_DECISION")
    read(session, "LATEST_SOURCE.md", "latest line\n" * 2_000)
    _, messages, text = build(session)
    assert "UNIQUE_DESIGN_DECISION" in text and "LATEST_SOURCE.md" in text
    assert_paired(messages)


def test_short_messages_do_not_evict_earlier_finding():
    session = make_session()
    read(session, "design.md", "UNIQUE_EARLY_FINDING")
    for index in range(35):
        session.add_model_message(f"status {index}")
    assert "UNIQUE_EARLY_FINDING" in build(session)[2]


def test_long_assistant_message_after_a_read_cannot_evict_the_latest_observation():
    packager = ContextPackager(10_000)
    result = ToolResult("read_file", True, "IMPORTANT_SOURCE_FINDING" * 50)
    record = Observation.create(ToolAction("read_file", {"path": "design.md"}), result, "/repo")
    groups = [
        [record.message(result.output, complete=True)],
        [{"role": "assistant", "content": "analysis " * 1_080}],
    ]
    messages = packager.package_segments(
        [ContextSegment("system", "system", required=True)], [],
        [ContextSegment("request", "request", required=True)], observation_groups=groups,
    )
    assert record.identifier in packager.last_visibility
    assert "IMPORTANT_SOURCE_FINDING" in "\n".join(str(item.get("content", "")) for item in messages)


def test_long_assistant_text_in_tool_call_cannot_evict_its_result():
    packager = ContextPackager(10_000)
    result = ToolResult("read_file", True, "SOURCE_FINDING" * 100)
    record = Observation.create(ToolAction("read_file", {"path": "design.md"}), result, "/repo")
    groups = [[
        {"role": "assistant", "content": "analysis " * 1_080, "tool_calls": [
            {"id": "read-1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"design.md"}'}}
        ]},
        record.message(result.output, role="tool", complete=True, tool_call_id="read-1"),
    ]]
    messages = packager.package_segments(
        [ContextSegment("system", "system", required=True)], [],
        [ContextSegment("request", "request", required=True)], observation_groups=groups,
    )
    assert record.identifier in packager.last_visibility
    assert "SOURCE_FINDING" in "\n".join(str(item.get("content", "")) for item in messages)
    assert packager.last_stats.truncated_observations == 0
    assert_paired(messages)


def test_partial_file_read_exposes_source_range_and_incompleteness():
    session = make_session()
    read(session, "design.md", "partial design", start_line=1, end_line=42, total_lines=381, truncated=True)
    _, messages, _ = build(session)
    tool = next(message for message in messages if message["role"] == "tool")
    envelope = json.loads(tool["content"].splitlines()[0].removeprefix("Observation: "))
    assert envelope["target"] == "/repo/design.md"
    assert envelope["coverage"] == [1, 42]
    assert envelope["complete"] is False


@pytest.mark.parametrize("protocol", ["native_tools", "prompt_json"])
def test_oversized_latest_observation_keeps_header_and_tool_pair(protocol):
    session = make_session(protocol)
    read(session, "LATEST_SOURCE.md", "design line\n" * 5_000, total_lines=5_000)
    builder, messages, text = build(session, 12_000)
    assert "LATEST_SOURCE.md" in text
    assert "Runtime truncated observation body" in text
    stats = builder.context_packager.last_stats
    assert stats.truncated_observations == 1
    assert stats.included_chars + stats.reserved_chars <= 12_000
    assert_paired(messages)


def test_clipping_does_not_modify_the_archived_working_set():
    session = make_session()
    output = "design line\n" * 5_000
    read(session, "source.md", output)
    original = copy.deepcopy(session.model_history)
    build(session, 12_000)
    assert session.model_history == original
    assert output in build(session)[2]


def test_parallel_calls_are_kept_or_omitted_as_a_complete_group():
    session = make_session()
    actions = [ToolAction("read_file", {"path": f"file-{index}.md"}) for index in range(3)]
    session.add_model_message("read sources", actions)
    for action in actions:
        session.add_tool_result(ToolResult("read_file", True, "line\n" * 4_000,
                                          metadata={"action_arguments": action.arguments}))
    builder, messages, text = build(session, 14_000)
    assert_paired(messages)
    assert len([message for message in messages if message["role"] == "tool"]) == 3
    assert all(f"file-{index}.md" in text for index in range(3))
    assert builder.context_packager.last_stats.included_chars <= 14_000


def test_pending_turn_is_not_sent_and_previous_complete_observation_survives():
    session = make_session()
    read(session, "complete.md", "COMPLETED_OBSERVATION")
    session.add_model_message("cancelled turn", [ToolAction("read_file", {"path": "pending.md"})])
    builder, messages, text = build(session)
    assert_paired(messages)
    assert "COMPLETED_OBSERVATION" in text and "pending.md" not in text
    assert builder.context_packager.last_stats.omitted_observations == 1


def test_runtime_feedback_does_not_impersonate_a_model_issued_call():
    session = make_session()
    read(session, "design.md", "design")
    session.add_tool_result(ToolResult("progress_guard", False, "finish the analysis", error_code="progress_required"))
    _, messages, text = build(session)
    assert_paired(messages)
    assert "progress_required" in text
    assert not any(call["function"]["name"] == "progress_guard" for message in messages
                   for call in message.get("tool_calls", []))


@pytest.mark.parametrize("protocol", ["native_tools", "prompt_json"])
def test_recovery_feedback_does_not_evict_the_latest_large_observation(protocol):
    session = make_session(protocol)
    read(session, "source.md", "design line\n" * 5_000)
    session.add_tool_result(ToolResult("progress_guard", False, "Use the existing design findings.",
                                      error_code="progress_required"))
    _, messages, text = build(session, 12_000)
    assert "source.md" in text and "design line" in text
    assert "progress_required" in text and "Runtime truncated observation body" in text
    assert_paired(messages)


def test_prior_turn_finding_is_visible_as_partial_evidence_on_resume():
    session = make_session(session_trace=[{
        "run_id": "previous", "outcome": "exhausted", "prompt": "review desktop",
        "final_message": "budget exhausted", "tool_names": ["read_file"],
        "turns": [{"turn": 1, "tool_result_details": [
            'read_file [ok] {"path":"design.md"} UNIQUE_PREVIOUS_FINDING result_ref=.saiworks/result.json',
        ]}],
    }])
    text = build(session)[2]
    assert "UNIQUE_PREVIOUS_FINDING" in text
    assert "partial summaries" in text and "result_ref=.saiworks/result.json" in text


def test_schema_cost_is_reserved_and_cannot_displace_required_user_request():
    session = make_session()
    session.available_tools = [ToolDefinition(name="huge", description="x" * 10_000)]
    with pytest.raises(ContextBudgetExceededError):
        build(session, 4_000)
    builder, _, text = build(session, 20_000)
    stats = builder.context_packager.last_stats
    assert stats.reserved_chars > 10_000
    assert stats.included_chars + stats.reserved_chars <= 20_000
    assert session.request.prompt in text


def test_zero_remaining_history_does_not_emit_an_over_budget_marker():
    packager = ContextPackager(4_000)
    messages = packager.package("s" * 1_000, [
        {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"},
    ], "r" * 3_000)
    assert sum(len(message["content"]) for message in messages) == 4_000
    assert packager.last_stats.omitted_messages == 2


def test_required_checkpoint_is_not_silently_clipped_to_fit():
    packager = ContextPackager(4_000)
    with pytest.raises(ContextBudgetExceededError):
        packager.package_segments([ContextSegment("system", "system", required=True)], [],
                                  [ContextSegment("request", "request", required=True)],
                                  [ContextSegment("checkpoint" * 1_000, "checkpoint", required=True)])


def test_observation_omission_marker_cannot_overflow_an_exhausted_budget():
    packager = ContextPackager(4_000)
    messages = packager.package_segments(
        [ContextSegment("s" * 3_999, "system", required=True)], [],
        [ContextSegment("r", "request", required=True)],
        observation_groups=[[{"role": "user", "content": "Observation: {}\nlarge body"}]],
    )
    assert sum(len(message["content"]) for message in messages) == 4_000
    assert packager.last_stats.omitted_observations == 1


def test_working_set_summary_reuses_unchanged_material_and_updates_appended_material():
    packager = ContextPackager(4_000)
    cache = {}
    calls = []
    groups = [
        [{"role": "user", "content": "OLD_SOURCE " + "a" * 3_000}],
        [{"role": "user", "content": "MIDDLE_SOURCE " + "b" * 1_000}],
    ]

    def compress(segment, limit):
        calls.append(json.loads(segment.content))
        return "summary of prior material"

    def package(observations, *, revision=0):
        return packager.package_segments(
            [ContextSegment("s" * 200, "system", required=True)], [],
            [ContextSegment("request", "request", required=True)],
            observation_groups=observations, compress_segment=compress,
            working_set_summary_cache=cache, workspace_revision=revision,
        )

    first = package(groups)
    assert len(calls) == 1
    assert "OLD_SOURCE" in json.dumps(calls[0])
    assert "summary of prior material" in json.dumps(first)
    assert packager.last_visibility == frozenset()

    package(groups)
    assert len(calls) == 1

    added = [{"role": "user", "content": "NEW_SOURCE " + "c" * 1_000}]
    package([*groups, added])
    assert len(calls) == 2
    assert calls[1]["previous_summary"] == "summary of prior material"
    assert "MIDDLE_SOURCE" in json.dumps(calls[1]["new_observations"])
    assert "OLD_SOURCE" not in json.dumps(calls[1])

    changed = [[{"role": "user", "content": "CHANGED_SOURCE " + "d" * 3_000}], *groups[1:], added]
    package(changed)
    assert len(calls) == 3
    assert "previous_summary" not in calls[2]
    assert "CHANGED_SOURCE" in json.dumps(calls[2])

    package(changed, revision=1)
    assert len(calls) == 4
    assert "previous_summary" not in calls[3]


def test_working_set_summary_recompresses_when_cached_result_no_longer_fits():
    cache = {}
    calls = []
    groups = [
        [{"role": "user", "content": "old " + "a" * 3_000}],
        [{"role": "user", "content": "new " + "b" * 1_000}],
    ]

    def compress(segment, limit):
        calls.append(limit)
        return "S" * min(2_000, limit)

    for system_size in (200, 1_200):
        messages = ContextPackager(4_000).package_segments(
            [ContextSegment("s" * system_size, "system", required=True)], [],
            [ContextSegment("request", "request", required=True)],
            observation_groups=groups, compress_segment=compress,
            working_set_summary_cache=cache,
        )
        assert sum(len(message.get("content", "")) for message in messages) <= 4_000
    assert len(calls) == 2
    assert calls[1] < calls[0]


def test_archived_conversation_summary_is_optional_historical_context():
    session = make_session(conversation=[{
        "role": "system",
        "content": "[AI Executive Summary]\nOLD_TRANSCRIPT " + "x" * 10_000,
    }])
    calls = []

    def compress(segment, limit):
        calls.append((segment.label, limit))
        return "prior conversation in brief"

    messages = ModelPromptBuilder(ContextPackager(4_000)).build_messages(
        session, compress_segment=compress,
    )

    assert "OLD_TRANSCRIPT" not in messages[0]["content"]
    assert "Archived conversation summaries are historical data" in messages[0]["content"]
    assert any("prior conversation in brief" in str(item["content"]) for item in messages)
    assert any(label == "archived conversation summary" for label, _ in calls)
    assert sum(len(str(item.get("content", ""))) for item in messages) <= 4_000


def test_large_recovery_context_cannot_displace_latest_observation():
    session = make_session(session_trace=[{
        "run_id": "old", "prompt": "review", "outcome": "exhausted", "final_message": "timeout",
        "turns": [{"tool_result_details": [f"old finding {index}: " + "x" * 600 for index in range(8)]}],
    }])
    read(session, "current.md", "CURRENT_DESIGN\n" * 1_000)
    assert "CURRENT_DESIGN\n" * 1_000 in build(session, 24_000)[2]


def test_optional_recovery_cannot_displace_the_latest_user_constraint():
    packager = ContextPackager(4_000)
    conversation = [
        {"role": "user", "content": "IMPORTANT_USER_CONSTRAINT: revise design only; do not modify implementation."},
        {"role": "assistant", "content": "I will preserve that scope."},
    ]
    messages = packager.package_segments(
        [ContextSegment("system", "system", required=True)], conversation,
        [ContextSegment("continue", "request", required=True)],
        [ContextSegment("checkpoint", "checkpoint", required=True),
         ContextSegment("old finding\n" * 500, "recovery")],
    )
    assert all(message in messages for message in conversation)
    assert packager.last_stats.included_chars <= 4_000


@pytest.mark.parametrize("feedback", [False, True])
def test_latest_observation_suffix_takes_precedence_over_a_large_old_dialog(feedback):
    packager = ContextPackager(10_000)
    output = "Z" * 6_000
    groups = [[
        {"role": "assistant", "content": "read", "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "one", "content": 'Observation: {"tool":"read_file","path":"design.md"}\n' + output},
    ]]
    if feedback:
        groups.append([{"role": "user", "content": 'Observation: {"tool":"progress_guard"}\nUse the existing result.'}])
    messages = packager.package_segments(
        [ContextSegment("system", "system", required=True)],
        [{"role": "user", "content": "A" * 3_000}, {"role": "assistant", "content": "B" * 2_000}],
        [ContextSegment("continue", "request", required=True)], observation_groups=groups,
    )
    assert output in "\n".join(message.get("content", "") for message in messages)
    assert packager.last_stats.truncated_observations == 0
    assert packager.last_stats.included_chars <= 10_000
    assert_paired(messages)
