import pytest


def test_execution_receipt_is_not_overwritten_by_the_next_tool_call(tmp_path):
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.tools.base import ExecutionPhase, SimpleTool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ToolAction, ToolResult
    tools = ToolRegistry(InMemoryLogger(base_dir=str(tmp_path / 'runs')))
    def handler(action, context):
        if action.arguments.get('start'):
            context.effect_tracker.begin()
        return ToolResult('effect', False, 'failed')
    tools.register(SimpleTool('effect', 'effect', {}, handler, risk_level='write', tracks_effects=True,
                              input_schema={'type': 'object', 'properties': {'start': {'type': 'boolean'}}}))
    before = tools.execute_tracked(ToolAction('effect'), cwd=str(tmp_path))
    after = tools.execute_tracked(ToolAction('effect', {'start': True}), cwd=str(tmp_path))
    assert before.phase is ExecutionPhase.NOT_STARTED
    assert after.phase is ExecutionPhase.STARTED


def test_same_registry_rejects_concurrent_tool_execution_without_blocking_cleanup(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from saiworks.observability.logger import InMemoryLogger
    from saiworks.tools.base import SimpleTool
    from saiworks.tools.registry import ToolRegistry
    from saiworks.types import ToolAction, ToolResult
    entered, release = Event(), Event()
    tools = ToolRegistry(InMemoryLogger(base_dir=str(tmp_path / 'runs')))
    def handler(*_):
        entered.set()
        assert release.wait(3)
        return ToolResult('slow', True, 'done')
    tools.register(SimpleTool('slow', 'slow', {}, handler))
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(tools.execute, ToolAction('slow'), cwd=str(tmp_path))
        assert entered.wait(3)
        try:
            with pytest.raises(RuntimeError, match='already executing'):
                tools.execute(ToolAction('slow'), cwd=str(tmp_path))
            tools.reset_state()
        finally:
            release.set()
        assert first.result(timeout=3).success
