from testcode.interaction.cli import CLI
from testcode.interaction.presenter import ConsolePresenter
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.engine import ExecutionEngine
from testcode.safety.guardrails import Guardrails
from testcode.safety.policy import DefaultPolicy
from testcode.sessions import SessionStore
from testcode.tools.builtin_provider import build_builtin_registry
from testcode.types import ModelReply, ToolAction, UserRequest


def engine_for(model, approvals):
    logger = InMemoryLogger()
    return ExecutionEngine(
        model=model, tools=build_builtin_registry(logger), logger=logger,
        guardrails=Guardrails(policy=DefaultPolicy(mode="auto"), logger=logger),
        approval_callback=lambda action, reason: approvals.append(action.name) or True,
    )


def test_chat_saves_authorization_and_restores_it_in_another_runtime(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    external = tmp_path / "external"
    origin.mkdir()
    external.mkdir()
    (external / "note.txt").write_text("external")

    class Model:
        def respond(self, session):
            action = (ToolAction("workspace_open", {"path": str(external)})
                      if session.request.prompt == "open"
                      else ToolAction("read_file", {"path": "note.txt"}))
            return ModelReply("Handled.", [action], done=True)

    store = SessionStore(tmp_path)
    approvals = []
    cli = CLI(engine_for(Model(), approvals), ConsolePresenter(), session_store=store)
    answers = iter(["open", "read", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    cli.chat(str(origin))
    saved = store.load(cli.active_session.session_id)
    assert saved.cwd == str(external)
    assert saved.workspace_state.origin_root == str(origin)
    assert saved.workspace_state.approved_roots == [str(external)]
    answers = iter(["read", "quit"])
    resumed = CLI(engine_for(Model(), approvals), ConsolePresenter(), session_store=store)
    resumed.chat(str(origin), session_id=saved.session_id, conversation=saved.messages)
    assert approvals == ["workspace_access"]


def test_workspace_roundtrip_reads_and_shell_use_current_directory(tmp_path):
    origin = tmp_path / "origin"
    other = tmp_path / "other"
    origin.mkdir()
    other.mkdir()
    (origin / "note.txt").write_text("origin")
    (other / "note.txt").write_text("other")
    actions = [
        ToolAction("read_file", {"path": "note.txt"}),
        ToolAction("shell_exec", {"command": "pwd"}),
        ToolAction("workspace_open", {"path": str(other)}),
        ToolAction("read_file", {"path": "note.txt"}),
        ToolAction("shell_exec", {"command": "pwd"}),
        ToolAction("workspace_open", {"path": str(origin)}),
        ToolAction("workspace_open", {"path": str(other)}),
        ToolAction("read_file", {"path": "note.txt"}),
    ]

    class Model:
        def respond(self, session):
            return ModelReply("Compared.", actions, done=True)

    summary = engine_for(Model(), []).execute(UserRequest("compare", str(origin)))
    assert all(result.success for result in summary.tool_results)
    assert [r.output for r in summary.tool_results if r.name == "read_file"] == ["origin", "other", "other"]
    assert [r.metadata["stdout"].strip() for r in summary.tool_results if r.name == "shell_exec"] == [str(origin), str(other)]


def test_resource_limited_child_can_request_effects(tmp_path):
    class Model:
        def respond(self, session):
            return ModelReply("Need write.", [ToolAction("subagent_request_effects", {"effects": ["write"]})], done=True)

    summary = engine_for(Model(), []).execute(UserRequest("fix", str(tmp_path), metadata={
        "delegated_task": {"allowed_effects": ["read"], "allowed_resources": ["src"]},
    }))
    assert summary.outcome == "blocked"
    assert summary.tool_results[0].metadata["requested_effects"] == ["write"]
