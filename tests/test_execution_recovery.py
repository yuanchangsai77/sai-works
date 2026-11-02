
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
        assert calls == []
        assert summary.outcome == 'runtime_error'
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


def test_partial_effect_allows_reads_but_never_implicitly_authorizes_more_writes(tmp_path):
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
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(mode='auto'), logger), logger)
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    runtime = Runtime(engine, logger=logger, task_store=store)
    with pytest.raises(OSError, match='result lost'):
        runtime.execute(UserRequest('create', str(tmp_path), {'task_id': 'task'}), submission_id='one')
    summary = runtime.execute(UserRequest('continue', str(tmp_path), {'task_id': 'task'}), submission_id='two')
    assert summary.outcome == 'runtime_error'
    assert calls == ['created', 'read']
    assert len(store.unknown_actions('task')) == 1
