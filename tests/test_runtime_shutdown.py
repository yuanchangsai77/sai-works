import pytest

from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionEngine
from saiworks.runtime import Runtime
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy
from saiworks.tools.registry import ToolRegistry
from saiworks.types import ModelReply, UserRequest


def test_close_retries_failed_cleanup_even_after_runtime_is_closed():
    class Tools:
        calls = 0
        def reset_state(self):
            self.calls += 1
            if self.calls == 1:
                raise OSError('close failed')
    class Engine:
        tools = Tools()
    runtime = Runtime(Engine())
    with pytest.raises(OSError, match='close failed'):
        runtime.close()
    assert runtime.is_closed
    runtime.close()
    assert runtime.engine.tools.calls == 2
    runtime.close()
    assert runtime.engine.tools.calls == 2


def test_force_close_cleanup_failure_preserves_interruption_and_can_be_retried(tmp_path):
    class Resource:
        fail = True
        def close(self):
            if self.fail:
                raise OSError('resource still alive')
    resource = Resource()
    logger = InMemoryLogger(base_dir=str(tmp_path / 'runs'))
    tools = ToolRegistry(logger)
    class Model:
        def respond(self, _session):
            tools.attach_state('resource', resource)
            runtime.close(force=True)
            return ModelReply('stopping', done=True)
    engine = ExecutionEngine(Model(), tools, Guardrails(DefaultPolicy(), logger), logger)
    runtime = Runtime(engine, logger=logger)
    with pytest.raises(KeyboardInterrupt):
        runtime.execute(UserRequest('work', str(tmp_path)))
    assert runtime.is_closed
    assert runtime.last_summary.outcome == 'interrupted'
    assert tools.state_for('resource') is resource
    resource.fail = False
    runtime.close()
    assert tools.state_for('resource') is None
