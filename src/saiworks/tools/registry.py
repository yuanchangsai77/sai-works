from __future__ import annotations

from pathlib import Path

from ..observations import Observation, ObservationArchive, observation_for
from ..types import ToolAction, ToolDefinition, ToolResult
from .base import ToolContext
from .result_packager import TRUNCATION_MARKER, ToolResultPackager
from .schema_validation import validate_schema


class ToolRegistry:
    def __init__(
        self,
        logger,
        max_output_bytes: int = 32_000,
        interceptors: list | None = None,
    ) -> None:
        self._tools = {}
        self._logger = logger
        self._state = {}
        self._persistent_state_names: set[str] = set()
        self._providers = []
        self._provider_tool_owners: dict[str, int] = {}
        self._max_output_bytes = max(1, int(max_output_bytes))
        self._result_packager = ToolResultPackager(self._max_output_bytes)
        self._interceptors = list(interceptors or [])
        self.observation_archive = ObservationArchive(logger)

    def register_interceptor(self, interceptor) -> None:
        self._interceptors.append(interceptor)

    def register(self, tool) -> bool:
        if tool.name in self._tools:
            self._logger.record(
                "tool.registration_conflict",
                {"name": tool.name, "error_code": "duplicate_tool_name"},
            )
            return False
        self._tools[tool.name] = tool
        return True

    def unregister(self, name: str) -> bool:
        if name not in self._tools:
            return False
        self._tools.pop(name, None)
        self._provider_tool_owners.pop(name, None)
        return True

    def state_for(self, name: str, default=None):
        return self._state.get(name, default)

    def attach_state(self, name: str, value, *, persistent: bool = False) -> None:
        self._state[name] = value
        if persistent:
            self._persistent_state_names.add(name)

    def attach_provider(self, provider) -> None:
        self._providers.append(provider)

    def refresh_providers(self) -> None:
        for provider in self._providers:
            provider_id = id(provider)
            discovered = {}
            for tool in provider.get_tools():
                if tool.name in discovered:
                    self._logger.record(
                        "tool.registration_conflict",
                        {"name": tool.name, "error_code": "duplicate_provider_tool_name"},
                    )
                    continue
                discovered[tool.name] = tool

            owned_names = {
                name for name, owner in self._provider_tool_owners.items() if owner == provider_id
            }
            for name in owned_names - discovered.keys():
                self._tools.pop(name, None)
                self._provider_tool_owners.pop(name, None)

            for name, tool in discovered.items():
                owner = self._provider_tool_owners.get(name)
                if owner == provider_id:
                    self._tools[name] = tool
                    continue
                if not self.register(tool):
                    continue
                self._provider_tool_owners[name] = provider_id

    def reset_state(self) -> None:
        for value in self._state.values():
            close = getattr(value, "close", None)
            if callable(close):
                close()
        self._state = {
            name: value
            for name, value in self._state.items()
            if name in self._persistent_state_names
        }

    def definitions(self) -> list[ToolDefinition]:
        self.refresh_providers()
        return [tool.definition() for tool in self._tools.values() if getattr(tool, "exposed", True)]

    def provider_statuses(self) -> list[dict[str, object]]:
        statuses: list[dict[str, object]] = []
        for provider in self._providers:
            get_statuses = getattr(provider, "get_statuses", None)
            if callable(get_statuses):
                statuses.extend(get_statuses())
        return statuses

    def definition_for(self, name: str) -> ToolDefinition | None:
        tool = self._tools.get(name)
        if tool is None:
            return None
        return tool.definition()

    def execute(self, action: ToolAction, *, cwd: str = ".", allowed_roots: list[str] | None = None) -> ToolResult:
        blocked = self.preflight(action, cwd=cwd, allowed_roots=allowed_roots)
        if blocked is not None:
            blocked = self._result_packager.package(blocked)
            if blocked.error_code != "blocked_by_security_policy":
                blocked.metadata.setdefault("action_arguments", dict(action.arguments))
            result_event = self._record_result(blocked)
            self._attach_action_artifact_ref(blocked, result_event)
            return blocked
        tool = self._tools[action.name]
        context = self._context(cwd, allowed_roots)
        action_event = self._logger.record(
            "tool.execute", {"name": action.name, "arguments": action.arguments}
        )
        observed_before = {
            path: len(entry.get("observations", []))
            for path, entry in self._state.get("read_files", {}).items()
        }
        result = tool.run(action, context)
        adapter = getattr(tool, "observation_adapter", None)
        spec = adapter(action, result, context) if callable(adapter) and result.success else None
        raw_output = result.output
        observation = Observation.create(action, result, str(Path(cwd).resolve()),
                                         str(self._state.get("active_session_id") or ""), spec)
        observation = self.observation_archive.save(observation, raw_output, action.arguments)
        result = self._result_packager.package(result)
        self._bind_visible_read_state(observed_before, observation.identifier, result, raw_output)
        result.metadata["_observation"] = observation.payload()
        result.metadata["_observation_body_complete"] = observation.complete and raw_output == result.output
        result.metadata.setdefault("action_arguments", dict(action.arguments))
        self._attach_action_artifact_ref(result, action_event)
        definition = tool.definition()
        # Runtime provenance is independent of a persistent shell's cwd.
        result.metadata["execution_workspace"] = str(Path(cwd).resolve())
        result.metadata["execution_risk"] = definition.risk_level
        if result.success:
            declared = definition.evidence_kinds
            existing = result.metadata.get("evidence", [])
            evidence = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
            result.metadata["evidence"] = list(dict.fromkeys([*evidence, *declared]))
            if definition.risk_level in {"write", "execute", "destructive"}:
                result.metadata["invalidates_workspace_state"] = True
        elif definition.risk_level == "test":
            result.metadata["invalidates_evidence"] = ["test"]
        result_event = self._record_result(result)
        self._attach_action_artifact_ref(result, result_event)
        return result

    def _bind_visible_read_state(
        self, before: dict[str, int], observation_id: str, result: ToolResult, raw_output: str,
    ) -> None:
        visible_lines: set[str] = set()
        if result.name == "search_text":
            output = result.output
            if output != raw_output:
                prefix = output.removesuffix(TRUNCATION_MARKER)
                visible_lines = {line.rstrip("\r\n") for line in prefix.splitlines(keepends=True)
                                 if line.endswith(("\n", "\r"))}
            else:
                visible_lines = set(output.splitlines())
        for path, entry in self._state.get("read_files", {}).items():
            for observed in entry.get("observations", [])[before.get(path, 0):]:
                observed["observation_id"] = observation_id
                if result.name == "read_file":
                    start = result.metadata.get("start_line", 1)
                    end = result.metadata.get("end_line", 0)
                    observed["lines"] = {line: content for line, content in observed["lines"].items()
                                         if start <= line <= end}
                elif result.name == "search_text":
                    observed["lines"] = {line: content for line, content in observed["lines"].items()
                                         if f"{path}:{line}:{content}" in visible_lines}

    def set_visible_observations(self, views: list[dict[str, object]]) -> None:
        """Keep patch authorization limited to lines present in the last model request."""
        by_id: dict[str, list[dict[str, object]]] = {}
        for view in views:
            identifier = view.get("observation_id")
            if isinstance(identifier, str):
                by_id.setdefault(identifier, []).append(view)
        # Mark individual read occurrences, since repeated identical reads share an ID.
        self._state["model_visible_reads_enforced"] = True
        for entry in self._state.get("read_files", {}).values():
            kept = []
            for observed in entry.get("observations", []):
                candidates = by_id.get(observed.get("observation_id"), [])
                if not candidates:
                    continue
                if any(view.get("complete") for view in candidates):
                    observed["model_visible"] = True
                    kept.append(observed)
                    continue
                ranges = [view.get("coverage") for view in candidates]
                ranges = [value for value in ranges if isinstance(value, (tuple, list))
                          and len(value) == 2 and all(type(part) is int for part in value)]
                visible = {line: content for line, content in observed.get("lines", {}).items()
                           if any(start <= line <= end for start, end in ranges)}
                if visible:
                    observed["lines"] = visible
                    observed["empty"] = False
                    observed["model_visible"] = True
                    kept.append(observed)
            entry["observations"] = kept

    def cached_result_valid(self, action: ToolAction, previous: ToolResult, *, cwd: str) -> bool:
        if not previous.success:
            return True  # Failure/permission retry rules remain engine-owned.
        record = observation_for(previous)
        tool = self._tools.get(action.name)
        # Operation deduplication protects side effects independently of observation novelty.
        if getattr(tool, "risk_level", "read") != "read":
            return True
        if not previous.metadata.get("_observation_body_complete", False):
            return False
        checker = getattr(tool, "observation_valid", None)
        return bool(record and record.workspace == str(Path(cwd).resolve()) and callable(checker)
                    and checker(record, action, self._context(cwd, None)))

    def preflight(
        self,
        action: ToolAction,
        *,
        cwd: str = ".",
        allowed_roots: list[str] | None = None,
    ) -> ToolResult | None:
        tool = self._tools.get(action.name)
        if tool is None:
            return ToolResult(
                name=action.name,
                success=False,
                output=f"unknown tool: {action.name}",
                error_code="unknown_tool",
            )

        validation_error = self._validate(action, getattr(tool, "input_schema", {}))
        if validation_error is not None:
            return validation_error

        context = self._context(cwd, allowed_roots)
        definition = tool.definition()
        for interceptor in self._interceptors:
            blocked = interceptor.before_execute(action, definition, context)
            if blocked is not None:
                return blocked
        return None

    def _context(
        self,
        cwd: str,
        allowed_roots: list[str] | None,
    ) -> ToolContext:
        return ToolContext(
            cwd=cwd,
            state=self._state,
            allowed_roots=list(allowed_roots or []),
            max_output_bytes=self._max_output_bytes,
        )

    def summarize_result(self, result: ToolResult) -> str:
        tool = self._tools.get(result.name)
        if tool is None:
            return result.output
        return tool.summarize(result)

    def _validate(self, action: ToolAction, schema: dict) -> ToolResult | None:
        issue = validate_schema(action.arguments, schema)
        if issue is None:
            return None
        if issue.keyword == "required":
            missing = list(issue.expected or [])
            return ToolResult(
                name=action.name,
                success=False,
                output=issue.message,
                error_code="missing_argument",
                metadata={"missing": missing},
            )
        if issue.keyword == "additionalProperties":
            unknown = list(issue.expected or [])
            return ToolResult(
                name=action.name,
                success=False,
                output=issue.message,
                error_code="unknown_argument",
                metadata={"unknown": unknown},
            )
        error_code = "invalid_argument_type" if issue.keyword == "type" else "invalid_argument_value"
        return ToolResult(
            name=action.name,
            success=False,
            output=f"argument '{issue.path or '<root>'}' {issue.message}",
            error_code=error_code,
            metadata={
                "argument": issue.path,
                "constraint": issue.keyword,
                "expected": issue.expected,
                "actual": issue.actual,
            },
        )

    def _record_result(self, result: ToolResult):
        return self._logger.record(
            "tool.result",
            {
                "name": result.name,
                "success": result.success,
                "output": result.output,
                "error_code": result.error_code,
                "metadata": result.metadata,
            },
        )

    def _attach_action_artifact_ref(self, result: ToolResult, event) -> None:
        action_ref = getattr(event, "payload", {}).get("arguments_ref", {})
        if isinstance(action_ref, dict) and action_ref.get("artifact_ref"):
            result.metadata.setdefault("action_artifact_ref", action_ref["artifact_ref"])
