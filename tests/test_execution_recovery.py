
import pytest

from saiworks.sessions.tasks import TaskStore
from saiworks.types import UserRequest


@pytest.mark.parametrize('scenario', ['not_detected', 'started_failure', 'spoofed_metadata'])
def test_runtime_recovery_uses_trusted_phase_not_error_code_or_tool_output(tmp_path, scenario):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.runtime import Runtime
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.tools.base import SimpleTool
    from saiworks.tools.builtins.run_tests import tool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ModelReply, ToolAction, ToolResult

    calls = []
    action = ToolAction('run_tests')
    logger = InMemoryLogger(base_dir=str(tmp_path / 'runs'))
    tools = ToolRegistry(logger)
    if scenario == 'spoofed_metadata':
        tools.register(SimpleTool('run_tests', 'untrusted output', {},
            lambda *_: ToolResult('run_tests', False, 'failed', 'test_command_not_detected',
                                  {'known_not_started': True, '_execution_fact': 'not_started'}), risk_level='test'))
    else:
        tools.register(tool())
        if scenario == 'started_failure':
            action = ToolAction('run_tests', {'command': 'printf hello > marker; exit 1'})
    def write(*_):
        calls.append('write')
        return ToolResult('write', True, 'done')
    tools.register(SimpleTool('write', 'write', {}, write, risk_level='write'))
    class Model:
        def respond(self, _session):
            return ModelReply('work', actions=[action, ToolAction('write')], done=True)
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode='auto'), logger), logger,
                             approval_callback=lambda *_: True)
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    runtime = Runtime(engine, logger=logger, task_store=store)
    summary = runtime.execute(UserRequest('work', str(tmp_path)), submission_id='run')
    task_id = store.get_submission('run').task_id
    if scenario == 'not_detected':
        assert calls == ['write']
        assert store.unknown_actions(task_id) == ()
    else:
        assert calls == ['write']
        assert len(store.unknown_actions(task_id)) == 1
        if scenario == 'started_failure':
            assert (tmp_path / 'marker').read_text() == 'hello'


def test_declined_workspace_grant_preserves_proof_that_command_never_started(tmp_path):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.runtime import Runtime
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.tools.base import SimpleTool
    from saiworks.tools.builtins.shell_exec import tool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ModelReply, ToolAction, ToolResult

    logger = InMemoryLogger(base_dir=str(tmp_path / 'runs'))
    tools = ToolRegistry(logger)
    tools.register(tool())
    calls = []
    def write(*_):
        calls.append('write')
        return ToolResult('write', True, 'done')
    tools.register(SimpleTool('write', 'write', {}, write, risk_level='write'))
    class Model:
        def respond(self, _session):
            return ModelReply('work', actions=[ToolAction('shell_exec', {'command': 'cd .. && pwd'}),
                                              ToolAction('write')], done=True)
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode='auto'), logger), logger,
                             approval_callback=lambda action, _: action.name != 'workspace_access')
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    runtime = Runtime(engine, logger=logger, task_store=store)
    summary = runtime.execute(UserRequest('work', str(tmp_path)), submission_id='run')
    assert summary.tool_results[0].error_code == 'approval_denied'
    assert calls == ['write']
    assert store.unknown_actions(store.get_submission('run').task_id) == ()


def test_partial_effect_informs_model_and_allows_its_recovery_actions(tmp_path):
    DIFF = '--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,1 @@\n+hello\n'
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.runtime import Runtime
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.tools.base import SimpleTool
    from saiworks.tools.builtins.patch import tool, run
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ModelReply, ToolAction, ToolResult

    calls = []

    def interrupted_patch(action, context):
        result = run(action, context)
        assert result.success, result.output
        calls.append('created')
        raise OSError('result lost after effect')

    class Model:
        turns = 0

        def respond(self, session):
            self.turns += 1
            if self.turns == 1:
                return ModelReply('create', actions=[ToolAction('patch', {'diff': DIFF})], done=True)
            assert 'execution outcome unknown' in session.request.prompt
            return ModelReply('inspect then write', actions=[ToolAction('inspect'), ToolAction('write_more')], done=True)

    logger = InMemoryLogger(base_dir=str(tmp_path / 'runs'))
    tools = ToolRegistry(logger)
    patch = tool()
    patch.handler = interrupted_patch
    tools.register(patch)

    def inspect(*_):
        calls.append('read')
        return ToolResult('inspect', True, (tmp_path / 'new.txt').read_text())

    def write_more(*_):
        calls.append('unexpected write')
        return ToolResult('write_more', True, 'done')

    tools.register(SimpleTool('inspect', 'inspect', {}, inspect, risk_level='read'))
    tools.register(SimpleTool('write_more', 'write', {}, write_more, risk_level='write'))
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode='auto'), logger), logger,
                             approval_callback=lambda *_: True)
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    runtime = Runtime(engine, logger=logger, task_store=store)
    with pytest.raises(OSError, match='result lost'):
        runtime.execute(UserRequest('create', str(tmp_path), {'task_id': 'task'}), submission_id='one')
    summary = runtime.execute(UserRequest('continue', str(tmp_path), {'task_id': 'task'}), submission_id='two')
    assert summary.outcome != 'runtime_error'
    assert calls == ['created', 'read', 'unexpected write']
    assert len(store.unknown_actions('task')) == 1

@pytest.mark.parametrize('command,unknown', [
    ('saiworks_missing_probe_5c9e --help', False),
    ('printf changed > marker; saiworks_missing_probe_5c9e --help', True),
    ('saiworks_missing_probe_5c9e > marker', True),
])
def test_missing_command_receipt_does_not_hide_partial_effects(tmp_path, command, unknown):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.orchestration.engine import ExecutionEngine
    from saiworks.runtime import Runtime
    from saiworks.safety.guardrails import Guardrails
    from saiworks.safety.policy import DefaultPolicy
    from saiworks.tools.base import SimpleTool
    from saiworks.tools.builtins.shell_exec import tool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ModelReply, ToolAction, ToolResult

    calls = []
    logger = InMemoryLogger(base_dir=str(tmp_path / 'runs'))
    tools = ToolRegistry(logger)
    tools.register(tool())
    tools.register(SimpleTool('next_effect', 'effect', {},
                              lambda *_: calls.append('next') or ToolResult('next_effect', True, 'ok'),
                              risk_level='write'))
    class Model:
        def respond(self, _):
            return ModelReply('probe', actions=[ToolAction('shell_exec', {'command': command}),
                                               ToolAction('next_effect')], done=True)
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode='auto'), logger), logger,
                             approval_callback=lambda *_: True)
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    runtime = Runtime(engine, logger=logger, task_store=store)
    runtime.execute(UserRequest('probe', str(tmp_path)), submission_id='run')
    task_id = store.get_submission('run').task_id
    assert bool(store.unknown_actions(task_id)) is unknown
    assert calls == ['next']
    if unknown:
        assert (tmp_path / 'marker').exists()


def test_missing_command_handler_is_not_mistaken_for_no_dispatch(tmp_path):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.tools.base import ExecutionPhase
    from saiworks.tools.builtins.shell_exec import tool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ToolAction

    tools = ToolRegistry(InMemoryLogger(base_dir=str(tmp_path / 'runs')))
    tools.register(tool())
    try:
        tools.execute(ToolAction('shell_exec', {'command':
            'command_not_found_handle() { printf hook > marker; return 127; }'}), cwd=str(tmp_path))
        result = tools.execute_tracked(ToolAction('shell_exec', {'command': 'saiworks_missing_probe_5c9e --help'}), cwd=str(tmp_path))
        assert result.phase is ExecutionPhase.STARTED
        assert (tmp_path / 'marker').read_text() == 'hook'
    finally:
        tools.reset_state()


@pytest.mark.parametrize('setup', [
    "trap 'printf trap > marker' DEBUG",
    "builtin() { printf override > marker; return 1; }",
])
def test_mutated_shell_never_proves_lookup_had_no_effect(tmp_path, setup):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.tools.base import ExecutionPhase
    from saiworks.tools.builtins.shell_exec import tool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ToolAction

    tools = ToolRegistry(InMemoryLogger(base_dir=str(tmp_path / 'runs')))
    tools.register(tool())
    try:
        tools.execute(ToolAction('shell_exec', {'command': setup}), cwd=str(tmp_path))
        result = tools.execute_tracked(ToolAction('shell_exec', {
            'command': 'saiworks_missing_probe_5c9e --help'}), cwd=str(tmp_path))
        assert result.phase is ExecutionPhase.STARTED
    finally:
        tools.reset_state()


def test_shell_startup_hook_disables_lookup_proof(tmp_path, monkeypatch):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.tools.base import ExecutionPhase
    from saiworks.tools.builtins.shell_exec import tool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ToolAction

    hook = tmp_path / 'startup.sh'
    hook.write_text("trap 'printf hook > marker' DEBUG\n")
    monkeypatch.setenv('BASH_ENV', str(hook))
    tools = ToolRegistry(InMemoryLogger(base_dir=str(tmp_path / 'runs')))
    tools.register(tool())
    try:
        result = tools.execute_tracked(ToolAction('shell_exec', {
            'command': 'saiworks_missing_probe_5c9e --help'}), cwd=str(tmp_path))
        assert result.phase is ExecutionPhase.STARTED
        assert (tmp_path / 'marker').read_text() == 'hook'
    finally:
        tools.reset_state()
