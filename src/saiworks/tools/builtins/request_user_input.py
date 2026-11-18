from __future__ import annotations

from ...types import ToolResult
from ..base import SimpleTool
from ..observation_adapters import control_observation
from ..shared import schema


def tool():
    return SimpleTool(
        name='request_user_input',
        description='Ask a concrete question and wait for the user decision before continuing this task.',
        arguments={'question': 'The complete question.', 'options': 'Optional suggested answers; free text remains allowed.'},
        input_schema=schema({'question': {'type': 'string', 'minLength': 1},
                             'options': {'type': 'array', 'items': {'type': 'string'}}}, required=['question']),
        handler=run, observation_adapter=control_observation,
    )


def run(action, context):
    if not action.arguments['question'].strip():
        return ToolResult(action.name, False, 'The question must contain text.', 'invalid_argument_value')
    handler = context.state.get('request_user_input')
    if handler is None:
        return ToolResult(action.name, False, 'This runtime has no user interaction channel.', 'interaction_unavailable')
    answer = handler('user', action, action.arguments['question'], context.cwd, '', action.arguments.get('options', []))
    return ToolResult(action.name, True, answer)
