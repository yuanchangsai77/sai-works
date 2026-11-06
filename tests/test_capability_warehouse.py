from __future__ import annotations

from dataclasses import dataclass
import json

import pytest

from saiworks.app import create_app
from saiworks.capabilities import (
    InstructionContent,
    LocalInstructionCapability,
    LocalToolboxSource,
    LocalToolboxSpec,
)
from saiworks.capabilities.model import (
    ActivatedCapability,
    CapabilityEntry,
    CapabilityManifest,
    ManifestItem,
)
from saiworks.capabilities.mcp_source import MCPToolboxSource
from saiworks.capabilities.warehouse import CapabilityWarehouse
from saiworks.mcp.config import MCPServerConfig
from saiworks.model.prompt import ModelPromptBuilder
from saiworks.context.packager import ContextPackager
from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.session import SessionContext
from saiworks.tools.base import SimpleTool
from saiworks.tools.registry import ToolRegistry
from saiworks.types import ModelReply, ToolAction, ToolDefinition, ToolResult, UserRequest


def _tool(name: str) -> SimpleTool:
    return SimpleTool(
        name=name,
        description=f"Tool {name}",
        arguments={},
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda action, _context: ToolResult(name=action.name, success=True, output="ok"),
    )


def test_generic_local_toolbox_supports_multiple_or_no_workflow_instructions():
    first = InstructionContent("local:review:plan", "plan", "PLAN FLOW")
    second = InstructionContent("local:review:verify", "verify", "VERIFY FLOW")
    source = LocalToolboxSource(
        (
            LocalToolboxSpec(
                id="local:review",
                name="review",
                source="local",
                description="Review workflows",
                instructions=(
                    LocalInstructionCapability(first.id, first.name, "Plan review", lambda: first),
                    LocalInstructionCapability(second.id, second.name, "Verify review", lambda: second),
                ),
            ),
            LocalToolboxSpec(
                id="local:tools-only",
                name="tools-only",
                source="local",
                description="No workflow instructions",
            ),
        )
    )

    review = source.open_toolbox("local:review")
    tools_only = source.open_toolbox("local:tools-only")

    assert [item.kind for item in review.items] == ["workflow_instructions", "workflow_instructions"]
    assert source.activate(second.id).instruction == second
    assert tools_only.items == ()


@dataclass
class FakeSource:
    tools: tuple[str, ...] = ("lookup",)
    toolbox_id: str = "fake:maps"
    open_calls: int = 0

    def catalog_entries(self):
        return (
            CapabilityEntry(
                id=self.toolbox_id,
                name=self.toolbox_id.split(":", 1)[-1],
                kind="toolbox",
                source="fake",
                description="Map lookup and routes",
                tags=("map", "route"),
            ),
        )

    def owns_toolbox(self, toolbox_id):
        return toolbox_id == self.toolbox_id

    def open_toolbox(self, toolbox_id):
        self.open_calls += 1
        return CapabilityManifest(
            toolbox_id=toolbox_id,
            name=self.toolbox_id.split(":", 1)[-1],
            source="fake",
            state="ready",
            origin="local",
            items=tuple(
                ManifestItem(
                    id=f"{self.toolbox_id}:{name}",
                    toolbox_id=toolbox_id,
                    name=name,
                    kind="tool",
                    description=f"Tool {name}",
                )
                for name in self.tools
            ),
        )

    def activate(self, capability_id):
        name = capability_id.rsplit(":", 1)[1]
        return ActivatedCapability(
            id=capability_id,
            toolbox_id=self.toolbox_id,
            kind="tool",
            tool=_tool(name),
        )


def _warehouse(source=None, **kwargs):
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)
    return CapabilityWarehouse([source or FakeSource()], registry, logger=logger, **kwargs), registry


def test_catalog_lists_described_toolboxes_without_opening_them():
    source = FakeSource()
    warehouse, registry = _warehouse(source)

    entries = warehouse.list_entries()

    assert [entry.id for entry in entries] == ["fake:maps"]
    assert entries[0].description == "Map lookup and routes"
    assert source.open_calls == 0
    assert registry.definition_for("lookup") is None
    status = warehouse.status("fake:maps")["entries"][0]
    assert status["lifecycle_state"] == "stored"
    assert status["health_state"] == "unknown"
    assert status["connection_state"] == "not_applicable"


def test_opened_items_exposes_only_already_opened_manifest_items():
    source = FakeSource(tools=("lookup", "route"))
    warehouse, _registry = _warehouse(source)

    assert warehouse.opened_items() == []

    warehouse.open_toolbox("fake:maps")

    assert [item.id for item in warehouse.opened_items()] == [
        "fake:maps:lookup",
        "fake:maps:route",
    ]
    assert source.open_calls == 1


def test_runtime_catalog_is_visible_without_opening_or_activating_tools():
    source = FakeSource()
    warehouse, registry = _warehouse(source)
    session = SessionContext(request=UserRequest(prompt="plan a route", cwd="/repo"))
    warehouse.apply_to_session(session)
    system = str(ModelPromptBuilder().build_messages(session)[0]["content"])

    assert '"id": "fake:maps"' in system
    assert "Map lookup and routes" in system
    assert "fake:maps:lookup" not in system
    assert source.open_calls == 0
    assert registry.definition_for("lookup") is None
    assert warehouse.active_ids() == []

    # Repeated model turns reuse the run snapshot; a new run sees catalog changes.
    source.catalog_entries = lambda: ()
    warehouse.apply_to_session(session)
    assert '"id": "fake:maps"' in session.capability_catalog
    next_session = SessionContext(request=UserRequest(prompt="hello", cwd="/repo"))
    warehouse.apply_to_session(next_session)
    assert next_session.capability_catalog == ""


def test_direct_answer_needs_no_discovery_or_activation(tmp_path, monkeypatch):
    app = create_app(workspace_root=tmp_path)
    source = FakeSource()
    app.engine.capability_warehouse = CapabilityWarehouse([source], app.engine.tools)
    calls = []

    def respond(session):
        calls.append(session)
        assert '"id": "fake:maps"' in session.capability_catalog
        assert "lookup" not in {tool.name for tool in session.available_tools}
        return ModelReply(message="hello", done=True)

    monkeypatch.setattr(app.engine.model, "respond", respond)
    summary = app.engine.execute(UserRequest(prompt="hello", cwd=str(tmp_path)))

    assert len(calls) == 1
    assert summary.final_message == "hello"
    assert summary.tool_results == []
    assert source.open_calls == 0


def test_runtime_catalog_bounds_entries_text_and_preserves_identifiers():
    source = FakeSource()
    source.catalog_entries = lambda: tuple(
        CapabilityEntry(
            id=f"fake:{index:02d}", name=f"box{index:02d}", kind="toolbox", source="fake",
            description='description "quoted"\n' * 200, enabled=index != 0,
            metadata={"target": "PRIVATE_TARGET"},
        ) for index in range(30)
    )
    warehouse, registry = _warehouse(source)
    summary = warehouse.model_catalog_summary()
    rows = [json.loads(line) for line in summary.splitlines() if line.startswith("{")]

    assert len(summary) <= 3_000
    assert 0 < len(rows) <= 8
    assert all(len(row["description"]) <= 160 for row in rows)
    assert [row["id"] for row in rows] == [f"fake:{index:02d}" for index in range(len(rows))]
    assert rows[0]["enabled"] is False
    assert "warehouse_list" in summary
    assert "PRIVATE_TARGET" not in summary
    assert source.open_calls == 0
    assert registry.definition_for("lookup") is None

    source.catalog_entries = lambda: (
        CapabilityEntry("fake:" + "x" * 4_000, "huge", "toolbox", "fake", "huge id"),
    )
    summary = warehouse.model_catalog_summary()
    assert len(summary) <= 3_000
    assert "warehouse_list" in summary
    assert not any(line.startswith("{") for line in summary.splitlines())


def test_small_context_omits_catalog_atomically_without_semantic_compression():
    session = SessionContext(request=UserRequest(prompt="hello", cwd="/repo"))
    session.available_tools = [ToolDefinition(name="warehouse_list", description="List catalog")]
    session.capability_catalog = "CATALOG_START\n" + "x" * 2_800 + "\nCATALOG_END"
    messages = ModelPromptBuilder(ContextPackager(4_000)).build_messages(session)
    system = str(messages[0]["content"])

    assert "CATALOG_START" not in system
    assert "CATALOG_END" not in system
    assert "warehouse_list" in system
    assert sum(len(str(item.get("content", ""))) for item in messages) <= 4_000


def test_activation_target_expands_one_toolbox_to_its_manifest_items():
    source = FakeSource(tools=("lookup", "route"))
    warehouse, _registry = _warehouse(source)

    capability_ids = warehouse.expand_activation_targets(["fake:maps"])

    assert capability_ids == ["fake:maps:lookup", "fake:maps:route"]
    assert source.open_calls == 1

    warehouse.activate(capability_ids)

    assert warehouse.active_toolbox_ids() == ["fake:maps"]
    assert warehouse.expand_release_targets(["fake:maps"]) == capability_ids


def test_mcp_declared_capabilities_form_the_outer_description():
    source = MCPToolboxSource(
        configs=(
            MCPServerConfig(
                name="amap",
                transport="stdio",
                command="amap",
                capabilities=("地图搜索", "路线规划"),
            ),
        ),
        discovery=object(),
        manager=object(),
    )

    entry = source.catalog_entries()[0]

    assert entry.description == "Provides 地图搜索, 路线规划."


def test_lifecycle_records_open_activate_use_and_release_states():
    warehouse, registry = _warehouse()
    warehouse.open_toolbox("fake:maps")
    warehouse.activate(["fake:maps:lookup"], reason="route lookup")

    assert warehouse.status("fake:maps")["entries"][0]["lifecycle_state"] == "activated"
    assert registry.definition_for("lookup") is not None

    warehouse.mark_used("lookup", success=False, error_code="remote_error")
    active = warehouse.status("fake:maps")["active"][0]
    assert active["state"] == "used"
    assert active["use_count"] == 1
    assert active["last_success"] is False
    assert active["last_error_code"] == "remote_error"

    warehouse.release(["fake:maps:lookup"], reason="step complete")
    status = warehouse.status("fake:maps")
    assert registry.definition_for("lookup") is None
    assert status["released"][0]["state"] == "released"
    assert status["released"][0]["release_reason"] == "step complete"


def test_missing_toolbox_and_leaf_descriptions_receive_fallbacks():
    source = FakeSource()
    source.catalog_entries = lambda: (
        CapabilityEntry("fake:maps", "maps", "toolbox", "fake", ""),
    )
    source.open_toolbox = lambda toolbox_id: CapabilityManifest(
        toolbox_id=toolbox_id,
        name="maps",
        source="fake",
        state="ready",
        items=(ManifestItem("fake:maps:lookup", toolbox_id, "lookup", "tool", ""),),
    )
    warehouse, _ = _warehouse(source)

    assert warehouse.list_entries()[0].description
    assert warehouse.open_toolbox("fake:maps").items[0].description


def test_catalog_pagination_uses_same_sequence_for_disabled_entries():
    class ManySource(FakeSource):
        def catalog_entries(self):
            return tuple(
                CapabilityEntry(
                    f"fake:{index:02}",
                    f"box-{index:02}",
                    "toolbox",
                    "fake",
                    f"Toolbox {index}",
                    enabled=index != 5,
                )
                for index in range(22)
            )

    warehouse, _ = _warehouse(ManySource())

    first_page = warehouse.list_entries(offset=0, max_results=20)
    second_page = warehouse.list_entries(offset=20, max_results=20)

    assert [entry.id for entry in first_page][-1] == "fake:19"
    assert [entry.id for entry in second_page] == ["fake:20", "fake:21"]
    assert first_page[5].enabled is False


def test_activation_is_transactional_when_a_tool_name_conflicts():
    warehouse, registry = _warehouse(FakeSource(tools=("fresh", "existing")))
    assert registry.register(_tool("existing")) is True
    warehouse.open_toolbox("fake:maps")

    with pytest.raises(ValueError, match="name conflict"):
        warehouse.activate(["fake:maps:fresh", "fake:maps:existing"])

    assert registry.definition_for("fresh") is None
    assert warehouse.status("fake:maps")["active"] == []


def test_activation_rejects_duplicate_tool_names_within_one_batch():
    warehouse, registry = _warehouse(FakeSource(tools=("one", "two")))
    warehouse.open_toolbox("fake:maps")
    warehouse.sources[0].activate = lambda capability_id: ActivatedCapability(
        id=capability_id,
        toolbox_id="fake:maps",
        kind="tool",
        tool=_tool("shared"),
    )

    with pytest.raises(ValueError, match="name conflict: shared"):
        warehouse.activate(["fake:maps:one", "fake:maps:two"])

    assert registry.definition_for("shared") is None
    assert warehouse.status("fake:maps")["active"] == []


def test_failed_activation_does_not_update_existing_scope_or_reason():
    logger = InMemoryLogger()
    warehouse = CapabilityWarehouse(
        [
            FakeSource(tools=("one",)),
            FakeSource(tools=("two",), toolbox_id="other:routes"),
        ],
        ToolRegistry(logger),
        logger=logger,
        max_active_capabilities=1,
    )
    warehouse.open_toolbox("fake:maps")
    warehouse.open_toolbox("other:routes")
    warehouse.activate(["fake:maps:one"], scope="run", reason="initial")

    with pytest.raises(ValueError, match="activation limit exceeded"):
        warehouse.activate(
            ["fake:maps:one", "other:routes:two"],
            scope="session",
            reason="failed update",
        )

    active = warehouse.status("fake:maps")["active"][0]
    assert active["scope"] == "run"
    assert active["reason"] == "initial"


def test_reactivation_updates_scope_and_reason():
    warehouse, _ = _warehouse()
    warehouse.open_toolbox("fake:maps")
    warehouse.activate(["fake:maps:lookup"], scope="run", reason="one request")

    record = warehouse.activate(
        ["fake:maps:lookup"],
        scope="session",
        reason="continued work",
    )[0]

    assert record.scope == "session"
    assert record.reason == "continued work"
    assert warehouse.persisted_capability_ids() == ["fake:maps:lookup"]


def test_mcp_catalog_target_removes_url_credentials_and_query():
    source = MCPToolboxSource(
        configs=(
            MCPServerConfig(
                name="remote",
                transport="streamable_http",
                url="https://user:password@example.test:8443/mcp?token=secret#fragment",
            ),
        ),
        discovery=object(),
        manager=object(),
    )

    entry = source.catalog_entries()[0]

    assert entry.metadata["target"] == "https://example.test:8443/mcp"


def test_session_capability_ids_restore_into_a_fresh_warehouse():
    first, _ = _warehouse()
    first.open_toolbox("fake:maps")
    first.activate(["fake:maps:lookup"], scope="session")

    restored, restored_registry = _warehouse()
    restored.restore_capabilities(first.persisted_capability_ids())

    assert restored.active_ids({"session"}) == ["fake:maps:lookup"]
    assert restored_registry.definition_for("lookup") is not None


def test_activation_budget_counts_toolboxes_not_leaf_tools():
    logger = InMemoryLogger()
    registry = ToolRegistry(logger)
    warehouse = CapabilityWarehouse(
        [
            FakeSource(tools=("one", "two")),
            FakeSource(tools=("three",), toolbox_id="other:routes"),
        ],
        registry,
        logger=logger,
        max_active_capabilities=1,
    )
    warehouse.open_toolbox("fake:maps")
    warehouse.open_toolbox("other:routes")

    warehouse.activate(["fake:maps:one", "fake:maps:two"])

    assert registry.definition_for("one") is not None
    assert registry.definition_for("two") is not None

    with pytest.raises(ValueError, match="activation limit exceeded"):
        warehouse.activate(["other:routes:three"])

    assert registry.definition_for("three") is None


def test_one_large_toolbox_uses_one_count_unit_but_keeps_leaf_schema_budget():
    tool_names = tuple(f"tool_{index}" for index in range(15))
    warehouse, registry = _warehouse(
        FakeSource(tools=tool_names),
        max_active_capabilities=8,
    )
    warehouse.open_toolbox("fake:maps")

    warehouse.activate([f"fake:maps:{name}" for name in tool_names])

    assert all(registry.definition_for(name) is not None for name in tool_names)
    budgets = warehouse.status()["budgets"]
    assert budgets["active_toolboxes"] == 1
    assert budgets["active_capabilities"] == 15


def test_only_session_scoped_skills_are_selected_for_persistence(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".saiworks" / "skills" / "route-guide"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: route-guide
description: Route planning guidance
---
ROUTE INSTRUCTIONS
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    app = create_app(workspace_root=tmp_path)
    warehouse = app.engine.capability_warehouse
    warehouse.open_toolbox("skill:route-guide")

    warehouse.activate(["skill:route-guide:instructions"], scope="run")
    assert warehouse.persisted_instructions() == []
    warehouse.release(reason="change scope")
    warehouse.activate(["skill:route-guide:instructions"], scope="session")
    assert [item.name for item in warehouse.persisted_instructions()] == ["route-guide"]


def test_skill_body_enters_prompt_only_after_leaf_activation(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".saiworks" / "skills" / "route-guide"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: route-guide
description: Route planning guidance
triggers: ["route"]
version: 1.0.0
---
PRIVATE ROUTE INSTRUCTIONS
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    app = create_app(workspace_root=tmp_path)
    warehouse = app.engine.capability_warehouse
    session = SessionContext(request=UserRequest(prompt="route", cwd=str(tmp_path)))

    warehouse.apply_to_session(session)
    initial_prompt = str(ModelPromptBuilder().build_messages(session)[0]["content"])
    assert "skill:route-guide:instructions" not in initial_prompt
    assert "PRIVATE ROUTE INSTRUCTIONS" not in initial_prompt

    warehouse.open_toolbox("skill:route-guide")
    warehouse.activate(["skill:route-guide:instructions"])
    warehouse.apply_to_session(session)
    active_prompt = str(ModelPromptBuilder().build_messages(session)[0]["content"])
    assert "PRIVATE ROUTE INSTRUCTIONS" in active_prompt


def test_builtin_skill_toolboxes_group_specialized_runtime_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    app = create_app(workspace_root=tmp_path)
    warehouse = app.engine.capability_warehouse
    initial_names = {definition.name for definition in app.engine.tools.definitions()}

    assert {"git_status", "git_diff"} <= initial_names
    assert {"git_show", "run_tests"}.isdisjoint(initial_names)

    git_manifest = warehouse.open_toolbox("skill:git-helper")
    assert [item.name for item in git_manifest.items] == [
        "instructions",
        "git_show",
    ]
    warehouse.activate([item.id for item in git_manifest.items], scope="run", reason="git workflow")
    active_names = {definition.name for definition in app.engine.tools.definitions()}
    assert {"git_status", "git_diff", "git_show"} <= active_names
    assert "run_tests" not in active_names

    warehouse.release(reason="switch workflows")
    active_names = {definition.name for definition in app.engine.tools.definitions()}
    assert {"git_status", "git_diff"} <= active_names
    assert "git_show" not in active_names

    pytest_manifest = warehouse.open_toolbox("skill:pytest-helper")
    assert [item.name for item in pytest_manifest.items] == ["instructions", "run_tests"]
    warehouse.activate([item.id for item in pytest_manifest.items], scope="run", reason="test workflow")
    active_names = {definition.name for definition in app.engine.tools.definitions()}
    assert "run_tests" in active_names
    assert {"git_status", "git_diff"} <= active_names
    assert "git_show" not in active_names


def test_session_restores_skill_owned_tool_leaf(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIWORKS_MODEL_BASE_URL", "")
    first = create_app(workspace_root=tmp_path)
    manifest = first.engine.capability_warehouse.open_toolbox("skill:git-helper")
    git_show_id = next(item.id for item in manifest.items if item.name == "git_show")
    first.engine.capability_warehouse.activate([git_show_id], scope="session")

    restored = create_app(workspace_root=tmp_path)
    restored.engine.capability_warehouse.restore_capabilities([git_show_id])

    assert restored.engine.tools.definition_for("git_show") is not None
    assert restored.engine.capability_warehouse.persisted_capability_ids() == [git_show_id]
