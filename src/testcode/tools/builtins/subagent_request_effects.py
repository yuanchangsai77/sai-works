from __future__ import annotations

from ...types import ToolAction, ToolResult
from ..base import SimpleTool, ToolContext
from ..shared import schema


VALID_EFFECTS = {"read", "write", "test", "execute", "network", "destructive"}


def tool() -> SimpleTool:
    return SimpleTool(
        name="subagent_request_effects",
        description="Request additional delegated effects from the parent agent without executing them.",
        arguments={"effects": "Effects needed to continue the delegated task."},
        input_schema=schema(
            {
                "effects": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(VALID_EFFECTS)},
                    "minItems": 1,
                }
            },
            required=["effects"],
        ),
        handler=run,
    )


def run(action: ToolAction, _context: ToolContext) -> ToolResult:
    effects = action.arguments.get("effects")
    if not isinstance(effects, list) or not effects or any(
        not isinstance(effect, str) or effect not in VALID_EFFECTS for effect in effects
    ):
        return ToolResult(
            action.name,
            False,
            "Requested effects must be a non-empty list of supported effects.",
            "invalid_argument_value",
        )
    requested = list(dict.fromkeys(effects))
    return ToolResult(
        action.name,
        False,
        "Additional delegated effects require parent review before this subagent can continue.",
        "delegated_capability_upgrade_requested",
        metadata={"requested_effects": requested},
    )
