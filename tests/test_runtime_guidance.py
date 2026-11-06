import pytest

from saiworks.capabilities.local_source import LocalToolCapability, LocalToolboxSource, LocalToolboxSpec
from saiworks.capabilities.tools import build_warehouse_tools
from saiworks.capabilities.warehouse import CapabilityWarehouse
from saiworks.model.prompt import ModelPromptBuilder
from saiworks.observability.logger import InMemoryLogger
from saiworks.observations import Observation
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.orchestration.guidance import select_operation_guidance
from saiworks.orchestration.session import SessionContext
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.tools.base import SimpleTool
from saiworks.tools.registry import ToolRegistry
from saiworks.types import ModelReply, ToolAction, ToolDefinition, ToolResult, UserRequest


def session_with(*tools):
    return SessionContext(
        request=UserRequest(prompt="hello", cwd="/repo"),
        available_tools=[ToolDefinition(name=name, description="Tool") for name in tools],
    )


def test_no_tools_omit_specialized_guidance_but_keep_control_and_security():
    session = session_with()
    assert select_operation_guidance(session) == ()
    system = str(ModelPromptBuilder().build_messages(session)[0]["content"])
    for absent in ("shell_exec keeps", "If patch returns", "completed subagent result", "Only activate"):
        assert absent not in system
    for retained in ("SEC-CREDENTIAL-001", "SEC-CLIENT-001", "approval_denied", "progress_required"):
        assert retained in system


@pytest.mark.parametrize(("tool", "included", "excluded"), [
    ("search_text", "counts only the returned lines", "If patch returns"),
    ("patch", "If patch returns file_changed_since_read", "shell_exec keeps"),
    ("shell_exec", "shell_exec keeps", "completed subagent result"),
    ("subagent_status", "completed subagent result", "If patch returns"),
    ("toolbox_open", "Only activate", "shell_exec keeps"),
])
def test_guidance_follows_current_tool_visibility(tool, included, excluded):
    text = "\n".join(select_operation_guidance(session_with(tool)))
    assert included in text
    assert excluded not in text


@pytest.mark.parametrize("recovered", [False, True])
def test_recovery_guidance_survives_tool_release(recovered):
    session = session_with()
    result = ToolResult("patch", False, "changed", error_code="file_changed_since_read")
    if recovered:
        session.recovered_observations = [Observation.create(ToolAction("patch", {}), result, "/repo")]
    else:
        session.tool_results = [result]
    assert "file_changed_since_read" in "\n".join(select_operation_guidance(session))


def test_runtime_refreshes_guidance_after_activation_and_release(tmp_path):
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)
    tool = SimpleTool(
        name="subagent_status", description="Status", arguments={},
        handler=lambda action, context: ToolResult(action.name, True, "done"),
    )
    source = LocalToolboxSource((LocalToolboxSpec(
        id="local:children", name="children", source="local", description="Child status",
        tools=(LocalToolCapability("local:children:status", tool),),
    ),))
    warehouse = CapabilityWarehouse([source], registry, logger=logger)
    for control in build_warehouse_tools(warehouse):
        registry.register(control)
    seen = []

    class Model:
        def respond(self, session):
            guidance = "\n".join(session.operation_guidance)
            seen.append("completed subagent result" in guidance)
            turn = len(seen)
            if turn == 1:
                return ModelReply(message="open", actions=[ToolAction("toolbox_open", {"toolbox_id": "local:children"})])
            if turn == 2:
                return ModelReply(message="activate", actions=[ToolAction("capability_activate", {
                    "capability_ids": ["local:children:status"],
                })])
            if turn == 3:
                assert "completed subagent result" in str(ModelPromptBuilder().build_messages(session)[0]["content"])
                return ModelReply(message="release", actions=[ToolAction("capability_release", {
                    "capability_ids": ["local:children:status"],
                })])
            return ModelReply(message="done", done=True)

    engine = ExecutionEngine(
        model=Model(), tools=registry, logger=logger, capability_warehouse=warehouse,
        guardrails=Guardrails(DefaultPolicy(mode="auto"), logger),
    )
    summary = engine.execute(UserRequest(prompt="check capabilities", cwd=str(tmp_path)))
    assert summary.outcome == "completed"
    assert all(result.success for result in summary.tool_results)
    assert seen == [False, False, True, False]
