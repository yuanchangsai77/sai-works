from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass


class ModelRetryableError(RuntimeError):
    """Raised when a model request can reasonably be attempted again."""

    retry_status = "Model request failed"


class ModelTimeoutError(ModelRetryableError):
    """Raised when one model API attempt exceeds its timeout."""

    retry_status = "Model request timed out"


class ModelConnectionError(ModelRetryableError):
    """Raised when a transient network failure interrupts a model request."""

    retry_status = "Model connection interrupted"


class ModelServiceError(ModelRetryableError):
    """Raised when the model service asks the client to retry later."""

    retry_status = "Model service temporarily unavailable"


class ModelProtocolError(RuntimeError):
    """Raised when a received model response violates the configured protocol."""


class ModelDecisionError(ModelProtocolError):
    """A rejected model decision; no action in its batch was dispatched."""

    def __init__(self, message: str, *, code: str = "invalid_model_response", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.tool_calls: list[dict] = []
        self.available_tools: list[str] = []
        self.request_id: str = ""

    def correction_messages(self, *, native_tools: bool) -> list[dict]:
        feedback = {
            "source": "runtime", "error_code": self.code, "message": str(self),
            "details": self.details, "available_tools": self.available_tools,
            "batch_executed": False,
            "instruction": "The entire decision batch was rejected before dispatch. Correct the decision using the advertised tools and schemas. Do not replay previously completed operations.",
        }
        calls = self.tool_calls
        identifiers = [item.get("id") for item in calls if isinstance(item, dict)]
        valid = bool(calls) and len(identifiers) == len(calls) and all(
            isinstance(identifier, str) and identifier for identifier in identifiers
        ) and len(set(identifiers)) == len(identifiers)
        if valid:
            for call in calls:
                function = call.get("function")
                if (call.get("type", "function") != "function" or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str) or not function["name"]
                        or not isinstance(function.get("arguments"), (str, dict))):
                    valid = False
                    break
                try:
                    arguments = function["arguments"]
                    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                    if not isinstance(parsed, dict):
                        valid = False
                        break
                    # Native history must itself contain valid JSON objects,
                    # including rejection of NaN/Infinity in nested values.
                    json.dumps(parsed, allow_nan=False)
                except (TypeError, ValueError):
                    valid = False
                    break
        if native_tools and valid:
            projected = deepcopy(calls)
            for call in projected:
                call["type"] = "function"
                if isinstance(call["function"]["arguments"], dict):
                    call["function"]["arguments"] = json.dumps(call["function"]["arguments"], ensure_ascii=False)
            return [
                {"role": "assistant", "content": None, "tool_calls": projected},
                *[{"role": "tool", "tool_call_id": call["id"],
                   "content": json.dumps({**feedback, "tool": call["function"]["name"]}, ensure_ascii=False)}
                  for call in calls],
            ]
        # Malformed IDs/envelopes cannot form a valid native tool exchange.
        # Omit that assistant envelope entirely; report rejection as runtime data.
        return [{"role": "user", "content": "Runtime model-decision feedback (not a new user request):\n"
                 + json.dumps(feedback, ensure_ascii=False)}]


@dataclass(frozen=True, slots=True)
class ModelClientConfig:
    base_url: str
    model: str = "gpt-5.4"
    timeout: float = 60.0
    stream_max_seconds: float = 900.0
    stream: bool = False


@dataclass(frozen=True, slots=True)
class ModelCapabilityProfile:
    model: str
    structured_output_mode: str = "prompt_json"
    native_tool_calls: bool = True
    # The runtime exposes one executable action channel per run.  Keep this
    # explicit instead of asking the model to choose between content actions
    # and API tool calls on every turn.
    action_protocol: str = "native_tools"
    parallel_tool_calls: bool = False
    context_budget_chars: int = 120_000
    provenance: str = "configured_default"
    verified: bool = False


@dataclass(frozen=True, slots=True)
class CleanedContent:
    message: str
    thinking: str = ""
    had_protocol_tags: bool = False
