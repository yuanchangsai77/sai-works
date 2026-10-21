"""Model working-set projections, separate from persisted user conversation."""
from __future__ import annotations

import hashlib
import json

from ..types import ToolResult


def bounded_arguments(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): bounded_arguments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [bounded_arguments(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"<omitted {len(value)} chars; sha256:{digest}>"
    return value


def observation_content(result: ToolResult) -> str:
    source = {"tool": result.name, "success": result.success}
    if result.error_code:
        source["error_code"] = result.error_code
    for key in (
        "action_arguments", "action_artifact_ref", "path", "cwd", "execution_workspace", "start_line",
        "end_line", "total_lines", "truncated", "output_original_bytes", "output_visible_bytes",
    ):
        if key in result.metadata:
            source[key] = bounded_arguments(result.metadata[key])
    return f"Observation: {json.dumps(source, ensure_ascii=False, sort_keys=True)}\n{result.output}"


def text_projection(group: list[dict[str, object]]) -> list[dict[str, object]]:
    """The content-action protocol uses the same atomic turn without API roles."""
    projected = []
    for message in group:
        role = message["role"]
        if role == "assistant":
            content = str(message.get("content") or "")
            if message.get("tool_calls"):
                content += "\nRequested calls: " + json.dumps(message["tool_calls"], ensure_ascii=False)
            projected.append({"role": "assistant", "content": content})
        else:
            projected.append({
                "role": "user",
                "content": str(message.get("content") or ""),
                **{key: value for key, value in message.items() if key.startswith("_")},
            })
    return projected
