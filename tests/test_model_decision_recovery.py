from copy import deepcopy
import json

import pytest

from saiworks.model.client import OpenAICompatibleModelClient
from saiworks.model.parser import ModelReplyParser
from saiworks.model.prompt import ModelPromptBuilder
from saiworks.model.types import ModelCapabilityProfile, ModelDecisionError, ModelProtocolError, ModelServiceError
from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.runtime import Runtime
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.sessions.tasks import TaskStore
from saiworks.tools.base import SimpleTool
from saiworks.tools.builtin_provider import build_builtin_registry
from saiworks.types import ModelReply, ToolAction, ToolResult, UserRequest


SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}},
          "required": ["value"], "additionalProperties": False}


def call(name="effect", arguments=None, identity="original-call"):
    return {"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps({"value": "ok"} if arguments is None else arguments)}}


def response(*calls, content=None):
    return {"choices": [{"finish_reason": "tool_calls" if calls else "stop", "message": {
        "role": "assistant", "content": content, **({"tool_calls": list(calls)} if calls else {})}}]}


def assert_native_history_valid(messages):
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            identity = message["tool_call_id"]
            assert identity in pending
            pending.remove(identity)
            continue
        assert not pending, "Tool results must complete before another message"
        calls = message.get("tool_calls", [])
        for item in calls:
            assert message["role"] == "assistant"
            assert isinstance(item["id"], str) and item["id"]
            assert item["id"] not in pending
            pending.add(item["id"])
            assert isinstance(item["function"]["arguments"], str)
            arguments = json.loads(item["function"]["arguments"])
            assert isinstance(arguments, dict)
            json.dumps(arguments, allow_nan=False)
    assert not pending


def harness(tmp_path, monkeypatch, replies, *, mode="full-access", attempts=0):
    logger = InMemoryLogger(str(tmp_path / "runs"))
    client = OpenAICompatibleModelClient(base_url="http://unused.invalid", logger=logger)
    requests, effects = [], []
    pending = iter(replies)

    def post(_url, payload, **_kwargs):
        assert_native_history_valid(payload["messages"])
        requests.append(deepcopy(payload))
        item = next(pending)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(client, "_post_json", post)
    tools = build_builtin_registry(logger)
    tools.register(SimpleTool("effect", "record effect", {},
        lambda action, _: effects.append(action.arguments["value"]) or ToolResult("effect", True, "recorded"),
        input_schema=SCHEMA, risk_level="execute"))
    engine = ExecutionEngine(client, tools, Guardrails(DefaultPolicy(mode), logger), logger,
                             max_model_attempts=attempts)
    runtime = Runtime(engine, logger=logger, task_store=TaskStore(tmp_path / "tasks.sqlite3"))
    return runtime, requests, effects, logger


@pytest.mark.parametrize("mixed", [False, True])
def test_unknown_tool_rejects_whole_batch_and_corrects_in_same_task(tmp_path, monkeypatch, mixed):
    invalid = [call("bash", identity="unknown-original")]
    if mixed:
        invalid.insert(0, call(identity="valid-but-rejected"))
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        response(*invalid), response(call(identity="corrected")), response(content="Completed."),
    ])
    try:
        task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="original")
        summary = runtime.advance(task.submission_id)
        assert summary.outcome == "completed"
        assert effects == ["ok"]
        assert len(requests) == 3
        correction = requests[1]["messages"][-(len(invalid) + 1):]
        assert correction[0]["tool_calls"] == invalid
        assert [item["tool_call_id"] for item in correction[1:]] == [item["id"] for item in invalid]
        for item in correction[1:]:
            feedback = json.loads(item["content"])
            assert feedback["error_code"] == "unknown_tool"
            assert feedback["batch_executed"] is False
            assert "effect" in feedback["available_tools"]
            assert "bash" not in feedback["available_tools"]
        assert len(summary.tool_results) == 1
        assert runtime.get_submission("original").task_id == task.task_id
        assert summary.checkpoint.objective == "work"
        assert "unknown-original" not in json.dumps(requests[2]["messages"])
        rejected, = [event for event in logger.events if event.name == "model.decision_rejected"]
        assert rejected.payload["task_id"] == task.task_id
        assert rejected.payload["request_id"]
        assert rejected.payload["tool_calls"] == invalid
    finally:
        runtime.close()


@pytest.mark.parametrize("arguments", [{}, {"value": 1}, {"value": "ok", "extra": True}])
def test_schema_error_rejects_valid_peer_call_before_any_dispatch(tmp_path, monkeypatch, arguments):
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [
        response(call(identity="valid"), call(arguments=arguments, identity="invalid")),
        response(call(identity="corrected")), response(content="Completed."),
    ])
    try:
        assert runtime.execute(UserRequest("work", str(tmp_path))).outcome == "completed"
        assert effects == ["ok"]
        feedback = json.loads(requests[1]["messages"][-1]["content"])
        assert feedback["error_code"] == "invalid_arguments"
        assert feedback["details"]["keyword"] in {"required", "type", "additionalProperties"}
    finally:
        runtime.close()


def test_repeated_protocol_error_has_separate_bounded_correction_budget(tmp_path, monkeypatch):
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [response(call("bash"))] * 4)
    runtime.engine.max_model_retries = 0
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "runtime_error"
        assert result.blockers[0].error_code == "model_protocol_error"
        assert effects == []
        assert len(requests) == 4  # Initial decision plus three corrections.
        assert result.checkpoint.runtime_state["model_attempts"] == "4"
        assert not any(event.name == "model.retry" for event in logger.events)
    finally:
        runtime.close()


def test_protocol_corrections_obey_existing_attempt_budget(tmp_path, monkeypatch):
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [response(call("bash"))] * 3, attempts=2)
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "exhausted"
        assert result.blockers[0].error_code == "model_attempt_budget_exhausted"
        assert len(requests) == 2
        assert not effects
    finally:
        runtime.close()


def test_correction_after_approval_does_not_replay_approved_effect(tmp_path, monkeypatch):
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [
        response(call(identity="approved-action")), response(call("bash")), response(content="Completed."),
    ], mode="auto")
    try:
        task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="approval-task")
        assert runtime.advance(task.submission_id).state == "waiting_approval"
        assert effects == []
        control = runtime.grant_task_control(task.task_id, {"approve"})
        approval, = runtime.pending_approvals(control)
        runtime.decide(control, approval.approval_id, "approve",
                       expected_action_fingerprint=approval.action_fingerprint)
        assert runtime.advance(task.submission_id).outcome == "completed"
        assert effects == ["ok"]
        assert len(requests) == 3
        assert runtime.task_store.get_approval(approval.approval_id).state == "consumed"
        assert not runtime.pending_approvals(control)
    finally:
        runtime.close()


@pytest.mark.parametrize("bad", [None, response(content=""), {"choices": []}])
def test_unparseable_decision_can_be_regenerated(tmp_path, monkeypatch, bad):
    if bad is None:
        bad = response(content='{"done":')
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [bad, response(content="Completed.")])
    try:
        assert runtime.execute(UserRequest("work", str(tmp_path))).outcome == "completed"
        assert len(requests) == 2
        assert not effects
        assert "Runtime model-decision feedback" in requests[1]["messages"][-1]["content"]
    finally:
        runtime.close()


def test_duplicate_call_ids_do_not_create_invalid_native_feedback():
    parser = ModelReplyParser()
    with pytest.raises(ModelDecisionError) as captured:
        parser.parse_response(response(call("bash"), call()), allowed_tool_names={"effect"})
    messages = captured.value.correction_messages(native_tools=True)
    assert [item["role"] for item in messages] == ["user"]
    assert "tool_calls" not in messages[0]


@pytest.mark.parametrize("arguments", ["{bad-json", "[]", "null", '{"value": NaN}', '{"value": Infinity}'])
def test_malformed_arguments_are_rejected_without_execution(tmp_path, monkeypatch, arguments):
    invalid = call()
    invalid["function"]["arguments"] = arguments
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [
        response(invalid), response(content="Completed."),
    ])
    try:
        assert runtime.execute(UserRequest("work", str(tmp_path))).outcome == "completed"
        assert not effects
        correction = requests[1]["messages"][-1]
        assert correction["role"] == "user"
        assert "tool_calls" not in correction
        assert json.loads(correction["content"].split("\n", 1)[1])["error_code"] == "invalid_arguments"
    finally:
        runtime.close()


def test_prompt_json_correction_uses_text_not_native_tool_messages(tmp_path, monkeypatch):
    invalid = response(content=json.dumps({"message": "work", "done": False,
                                          "actions": [{"name": "bash", "arguments": {}}]}))
    valid = response(content=json.dumps({"message": "Completed.", "done": True, "actions": []}))
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [invalid, valid])
    runtime.engine.model.capability_profile = ModelCapabilityProfile(model="test", action_protocol="prompt_json")
    try:
        assert runtime.execute(UserRequest("work", str(tmp_path))).outcome == "completed"
        assert not effects
        assert all(message["role"] != "tool" for message in requests[1]["messages"])
        assert "unknown_tool" in requests[1]["messages"][-1]["content"]
    finally:
        runtime.close()


def test_policy_rejection_is_tool_feedback_not_model_protocol_retry(tmp_path, monkeypatch):
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        response(call()), response(content="The action is blocked by policy."),
    ], mode="readonly")
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert not effects
        assert len(requests) == 2
        assert result.tool_results[0].error_code == "blocked_by_policy"
        assert not any(event.name == "model.decision_rejected" for event in logger.events)
    finally:
        runtime.close()


def test_cancellation_after_invalid_response_prevents_correction(tmp_path, monkeypatch):
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [])
    task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="cancel-original")

    def post(_url, payload, **_kwargs):
        requests.append(deepcopy(payload))
        runtime.cancel_submission(task.submission_id)
        return response(call("bash"))

    monkeypatch.setattr(runtime.engine.model, "_post_json", post)
    try:
        with pytest.raises(KeyboardInterrupt):
            runtime.advance(task.submission_id)
        assert runtime.get_submission(task.submission_id).result.outcome == "interrupted"
        assert len(requests) == 1
        assert not effects
    finally:
        runtime.close()


def test_correction_exchange_is_reserved_under_context_pressure():
    from saiworks.context.packager import ContextPackager
    from saiworks.orchestration.session import SessionContext

    parser = ModelReplyParser()
    with pytest.raises(ModelDecisionError) as captured:
        parser.parse_response(response(call("bash", identity="id-under-pressure")),
                              allowed_tool_names={"effect"})
    session = SessionContext(UserRequest("work", "/tmp"))
    session.model_history = [[{"role": "assistant", "content": "old history " * 30000}]]
    session.model_correction = captured.value.correction_messages(native_tools=True)
    builder = ModelPromptBuilder(ContextPackager(32000))
    messages = builder.build_messages(session)
    assert messages[-2]["tool_calls"][0]["id"] == "id-under-pressure"
    assert messages[-1]["tool_call_id"] == "id-under-pressure"
    assert "old history " * 100 not in json.dumps(messages)
    assert len(json.dumps(session.model_correction, ensure_ascii=False)) <= builder.context_packager.last_stats.reserved_chars


def test_existing_effects_are_not_replayed_while_correcting_next_batch(tmp_path, monkeypatch):
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [
        response(call(arguments={"value": "first"}, identity="first-effect")),
        response(call(arguments={"value": "second"}, identity="rejected-effect"), call("bash", identity="bad")),
        response(call(arguments={"value": "second"}, identity="resubmitted-effect")),
        response(content="Completed."),
    ])
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "completed"
        assert effects == ["first", "second"]
        assert len(result.tool_results) == 2
        assert len(requests) == 4
    finally:
        runtime.close()


def test_transport_retry_preserves_pending_correction_without_dispatch(tmp_path, monkeypatch):
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        response(call("bash")), ModelServiceError("temporarily unavailable"),
        response(call(identity="corrected")), response(content="Completed."),
    ])
    runtime.engine.model_retry_delays = (0.0,)
    try:
        assert runtime.execute(UserRequest("work", str(tmp_path))).outcome == "completed"
        assert effects == ["ok"]
        assert len(requests) == 4
        assert requests[1]["messages"][-2:] == requests[2]["messages"][-2:]
        assert len([event for event in logger.events if event.name == "model.retry"]) == 1
        assert len([event for event in logger.events if event.name == "model.decision_rejected"]) == 1
    finally:
        runtime.close()


def test_model_correction_does_not_bypass_existing_approval_denial(tmp_path, monkeypatch):
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [
        response(call()), response(call("bash")), response(call()), response(content="Cannot execute the denied action."),
    ], mode="auto")
    try:
        task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="denied-task")
        assert runtime.advance(task.submission_id).state == "waiting_approval"
        control = runtime.grant_task_control(task.task_id, {"approve"})
        approval, = runtime.pending_approvals(control)
        runtime.decide(control, approval.approval_id, "deny",
                       expected_action_fingerprint=approval.action_fingerprint)
        result = runtime.advance(task.submission_id)
        assert not effects
        assert len(requests) == 4
        assert [item.error_code for item in result.tool_results] == ["approval_denied", "approval_denied"]
        assert runtime.task_store.get_approval(approval.approval_id).state == "denied"
    finally:
        runtime.close()


@pytest.mark.parametrize("reason", ["content_filter", "error", "provider_policy_stop"])
@pytest.mark.parametrize("with_calls", [False, True])
def test_provider_stop_is_not_a_correctable_decision(tmp_path, monkeypatch, reason, with_calls):
    stopped = response(call()) if with_calls else response(content="Blocked.")
    stopped["choices"][0]["finish_reason"] = reason
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        stopped, response(content="This response must not be requested."),
    ])
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "runtime_error"
        assert result.blockers[0].error_code == "model_protocol_error"
        assert len(requests) == 1
        assert not effects
        assert not any(event.name == "model.decision_rejected" for event in logger.events)
    finally:
        runtime.close()


@pytest.mark.parametrize("stopped", [
    {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "Refused."}}]},
    {"error": {"code": "content_filter", "message": "Policy blocked the request"}},
    ModelProtocolError("Unclassified provider protocol failure"),
])
def test_explicit_refusal_and_unclassified_protocol_failure_do_not_retry(tmp_path, monkeypatch, stopped):
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        stopped, response(content="Do not request this."),
    ])
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "runtime_error"
        assert len(requests) == 1
        assert not effects
        assert not any(event.name == "model.decision_rejected" for event in logger.events)
    finally:
        runtime.close()


def test_policy_stop_during_correction_stops_further_requests(tmp_path, monkeypatch):
    stopped = response(content="Filtered.")
    stopped["choices"][0]["finish_reason"] = "content_filter"
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        response(call("bash")), stopped, response(content="Do not request this."),
    ])
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "runtime_error"
        assert len(requests) == 2
        assert not effects
        assert len([event for event in logger.events if event.name == "model.decision_rejected"]) == 1
    finally:
        runtime.close()


def test_truncated_response_is_correctable_but_not_dispatched(tmp_path, monkeypatch):
    truncated = response(call())
    truncated["choices"][0]["finish_reason"] = "length"
    runtime, requests, effects, _ = harness(tmp_path, monkeypatch, [
        truncated, response(content="Completed without executing the truncated decision."),
    ])
    try:
        assert runtime.execute(UserRequest("work", str(tmp_path))).outcome == "completed"
        assert len(requests) == 2
        assert not effects
        assert_native_history_valid(requests[1]["messages"])
        assert json.loads(requests[1]["messages"][-1]["content"])["error_code"] == "truncated_model_response"
    finally:
        runtime.close()


@pytest.mark.parametrize("events", [
    [{"error": {"code": "content_filter", "message": "Blocked"}}],
    [{"choices": [{"delta": {"refusal": "Refused"}, "finish_reason": "stop"}]}],
    [{"choices": [{"delta": {"tool_calls": [call()]}, "finish_reason": "content_filter"}]}],
])
def test_streamed_policy_stops_do_not_enter_correction(tmp_path, monkeypatch, events):
    import io

    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [])
    client = runtime.engine.model
    client.stream = True
    monkeypatch.setattr(client, "_post_json", OpenAICompatibleModelClient._post_json.__get__(client))
    monkeypatch.setattr(client, "_iter_sse_json_events", lambda *_args, **_kwargs: iter(events))

    def open_response(request, **_kwargs):
        payload = json.loads(request.data)
        assert_native_history_valid(payload["messages"])
        requests.append(payload)
        stream = io.BytesIO()
        stream.headers = {"Content-Type": "text/event-stream"}
        return stream

    monkeypatch.setattr(client, "_open_url", open_response)
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "runtime_error"
        assert len(requests) == 1
        assert not effects
        assert not any(event.name == "model.decision_rejected" for event in logger.events)
    finally:
        runtime.close()


@pytest.mark.parametrize("adapter", ["native", "direct"])
@pytest.mark.parametrize("hidden_first", [False, True])
@pytest.mark.parametrize("mode", ["auto", "full-access"])
def test_registered_hidden_tool_rejects_batch_before_effects_or_approval(
    tmp_path, monkeypatch, adapter, hidden_first, mode,
):
    invalid = [call(identity="legal"), call("hidden", identity="hidden")]
    if hidden_first:
        invalid.reverse()
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        response(*invalid), response(content="Use only visible tools."),
    ], mode=mode)
    runtime.engine.tools.register(SimpleTool("hidden", "not advertised", {},
        lambda *_: pytest.fail("Hidden tool was dispatched"), input_schema=SCHEMA, risk_level="execute"))
    definitions = runtime.engine._available_tool_definitions
    monkeypatch.setattr(runtime.engine, "_available_tool_definitions", lambda request: [
        item for item in definitions(request) if item.name != "hidden"
    ])
    if adapter == "direct":
        class DirectModel:
            calls = 0

            def respond(self, session):
                self.calls += 1
                assert "hidden" not in {tool.name for tool in session.available_tools}
                if self.calls == 1:
                    return ModelReply("bad batch", actions=[
                        ToolAction(item["function"]["name"], {"value": "ok"}) for item in invalid
                    ])
                assert session.tool_results == []
                feedback = json.loads(session.model_correction[-1]["content"])
                assert "hidden" not in feedback["available_tools"]
                assert feedback["batch_executed"] is False
                return ModelReply("Use only visible tools.", done=True)

        runtime.engine.model = DirectModel()
    try:
        task = runtime.accept(UserRequest("work", str(tmp_path)), submission_id="hidden-batch")
        result = runtime.advance(task.submission_id)
        assert result.outcome == "completed"
        assert result.tool_results == []
        assert not effects
        assert not runtime.task_store.snapshot(task.task_id).pending_interactions
        control = runtime.grant_task_control(task.task_id, {"approve"})
        assert not runtime.pending_approvals(control)
        assert len([event for event in logger.events if event.name == "model.decision_rejected"]) == 1
    finally:
        runtime.close()


def test_adapter_mutations_cannot_expand_tools_or_loosen_schema_in_correction(tmp_path, monkeypatch):
    runtime, _, effects, _ = harness(tmp_path, monkeypatch, [])
    hidden = SimpleTool("hidden", "hidden", {}, lambda *_: pytest.fail("Hidden dispatched"), input_schema=SCHEMA)
    runtime.engine.tools.register(hidden)
    definitions = runtime.engine._available_tool_definitions
    monkeypatch.setattr(runtime.engine, "_available_tool_definitions", lambda request: [
        item for item in definitions(request) if item.name != "hidden"
    ])

    class MutatingModel:
        calls = 0

        def respond(self, session):
            self.calls += 1
            assert "hidden" not in {tool.name for tool in session.available_tools}
            effect = next(tool for tool in session.available_tools if tool.name == "effect")
            assert effect.input_schema["properties"]["value"]["type"] == "string"
            if self.calls == 1:
                session.available_tools.append(hidden.definition())
                return ModelReply("expose hidden", actions=[ToolAction("effect", {"value": "bad"}), ToolAction("hidden", {"value": "ok"})])
            if self.calls == 2:
                effect.input_schema["properties"]["value"]["type"] = "integer"
                return ModelReply("loosen schema", actions=[ToolAction("effect", {"value": 1})])
            if self.calls == 3:
                return ModelReply("corrected", actions=[ToolAction("effect", {"value": "corrected"})])
            return ModelReply("Completed.", done=True)

    runtime.engine.model = MutatingModel()
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "completed"
        assert effects == ["corrected"]
        assert len(result.tool_results) == 1
    finally:
        runtime.close()


def test_visible_tool_still_enforces_delegated_resources_after_batch_validation(tmp_path, monkeypatch):
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [
        response(call()), response(content="The resource scope does not allow this action."),
    ])
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path), {"delegated_task": {
            "allowed_effects": ["read", "execute"], "allowed_resources": ["allowed"],
        }}))
        assert not effects
        assert len(requests) == 2
        assert result.tool_results[0].error_code == "delegated_resource_not_allowed"
        assert not any(event.name == "model.decision_rejected" for event in logger.events)
    finally:
        runtime.close()


@pytest.mark.parametrize("mutation", ["hidden", "schema"])
def test_native_parser_uses_advertised_snapshot_and_preserves_original_ids(tmp_path, monkeypatch, mutation):
    runtime, requests, effects, logger = harness(tmp_path, monkeypatch, [])
    hidden = SimpleTool("hidden", "hidden", {}, lambda *_: pytest.fail("Hidden dispatched"), input_schema=SCHEMA)
    runtime.engine.tools.register(hidden)
    definitions = runtime.engine._available_tool_definitions
    monkeypatch.setattr(runtime.engine, "_available_tool_definitions", lambda request: [
        item for item in definitions(request) if item.name != "hidden"
    ])

    def post(_url, payload, **_kwargs):
        assert_native_history_valid(payload["messages"])
        requests.append(deepcopy(payload))
        advertised = {tool["function"]["name"]: tool["function"]["parameters"] for tool in payload["tools"]}
        assert "hidden" not in advertised
        assert advertised["effect"]["properties"]["value"]["type"] == "string"
        if len(requests) == 1:
            session = runtime.engine.current_session
            if mutation == "hidden":
                session.available_tools.append(hidden.definition())
                return response(call("hidden", identity="provider-original"))
            effect = next(tool for tool in session.available_tools if tool.name == "effect")
            effect.input_schema["properties"]["value"]["type"] = "integer"
            return response(call(arguments={"value": 1}, identity="provider-original"))
        if len(requests) == 2:
            return response(call(identity="corrected"))
        return response(content="Completed.")

    monkeypatch.setattr(runtime.engine.model, "_post_json", post)
    try:
        result = runtime.execute(UserRequest("work", str(tmp_path)))
        assert result.outcome == "completed"
        assert effects == ["ok"]
        rejected, = [event for event in logger.events if event.name == "model.decision_rejected"]
        assert rejected.payload["tool_calls"][0]["id"] == "provider-original"
        assert rejected.payload["request_id"]
        assert requests[1]["messages"][-1]["tool_call_id"] == "provider-original"
    finally:
        runtime.close()
