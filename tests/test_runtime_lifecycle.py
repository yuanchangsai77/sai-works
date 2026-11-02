import pytest
from saiworks.observability.logger import InMemoryLogger
from saiworks.orchestration.engine import ExecutionContext, ExecutionEngine
from saiworks.safety.guardrails import Guardrails
from saiworks.safety.policy import DefaultPolicy


def test_stale_context_cancellation_cannot_touch_new_execution_resources():
    class Tools:
        calls = 0
        def reset_state(self):
            self.calls += 1

    logger = InMemoryLogger()
    tools = Tools()
    engine = ExecutionEngine(object(), tools, Guardrails(DefaultPolicy(), logger), logger)
    old, current = ExecutionContext(task_id='old'), ExecutionContext(task_id='current')
    engine._execution_context = current
    engine.cancel_execution(old)
    assert old.cancellation.is_set()
    assert not current.cancellation.is_set()
    assert tools.calls == 0
    engine.cancel_execution(current)
    assert current.cancellation.is_set()
    assert tools.calls == 1


def test_ordinary_cleanup_cannot_close_resources_owned_by_running_execution():
    class Tools:
        calls = 0
        def reset_state(self):
            self.calls += 1

    logger = InMemoryLogger()
    tools = Tools()
    engine = ExecutionEngine(object(), tools, Guardrails(DefaultPolicy(), logger), logger)
    engine._execution_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match='while execution is running'):
            engine.close_tool_state()
        assert tools.calls == 0
    finally:
        engine._execution_lock.release()
    engine.close_tool_state()
    assert tools.calls == 1


def test_cancel_closes_context_owned_tools_during_next_task_assembly():
    class Tools:
        calls = 0
        def reset_state(self):
            self.calls += 1

    logger = InMemoryLogger()
    old_tools, next_tools = Tools(), Tools()
    engine = ExecutionEngine(object(), old_tools, Guardrails(DefaultPolicy(), logger), logger)
    context = ExecutionContext(task_id='old', tools=old_tools)
    engine._execution_context = context
    # Runtime may assemble the next Task before execute_in_context binds it.
    engine.tools = next_tools
    engine.cancel_execution(context)
    assert old_tools.calls == 1
    assert next_tools.calls == 0


def test_context_binding_interruption_releases_execution_slot_without_touching_previous_context():
    from saiworks.types import UserRequest
    class InterruptedLock:
        def __enter__(self):
            raise KeyboardInterrupt
        def __exit__(self, *_):
            pass
    logger = InMemoryLogger()
    engine = ExecutionEngine(object(), object(), Guardrails(DefaultPolicy(), logger), logger)
    previous = engine._execution_context
    engine._tool_cleanup_lock = InterruptedLock()
    with pytest.raises(KeyboardInterrupt):
        engine.execute_in_context(UserRequest('work', '.'), ExecutionContext())
    assert engine._execution_context is previous
    assert not previous.cancellation.is_set()
    assert engine._execution_lock.acquire(blocking=False)
    engine._execution_lock.release()
