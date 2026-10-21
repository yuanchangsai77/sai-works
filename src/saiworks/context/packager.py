from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from ..observations import Observation

CURRENT_RUN_CONTEXT_PREFIX = (
    "Current run context (runtime-generated; not part of the user request):"
)


class ContextBudgetExceededError(RuntimeError):
    """The required instructions and current user request cannot fit together."""


@dataclass(frozen=True, slots=True)
class ContextPackageStats:
    budget_chars: int
    included_chars: int
    omitted_messages: int = 0
    truncated_system: bool = False
    truncated_current: bool = False
    omitted_observations: int = 0
    truncated_observations: int = 0
    reserved_chars: int = 0


@dataclass(frozen=True, slots=True)
class ContextSegment:
    content: str
    label: str
    priority: int = 50
    required: bool = False
    truncatable: bool = True
    compressible: bool = True


class ContextPackager:
    """Build a bounded prompt while preserving control-plane facts first."""

    def __init__(self, max_chars: int = 120_000) -> None:
        self.max_chars = max(4_000, int(max_chars))
        self.last_stats = ContextPackageStats(self.max_chars, 0)
        self.last_visibility: frozenset[str] = frozenset()
        self.last_views: list[dict[str, object]] = []

    def package(
        self,
        system_content: str,
        conversation: list[dict[str, object]],
        current_content: str,
    ) -> list[dict[str, object]]:
        return self.package_segments(
            [ContextSegment(system_content, "system", required=True)],
            conversation,
            [ContextSegment(current_content, "current request", priority=100, required=True)],
        )

    def package_segments(
        self,
        system_segments: list[ContextSegment],
        conversation: list[dict[str, object]],
        current_segments: list[ContextSegment],
        trailing_context_segments: list[ContextSegment] | None = None,
        observation_groups: list[list[dict[str, object]]] | None = None,
        reserved_chars: int = 0,
        compress_segment: Callable[[ContextSegment, int], str] | None = None,
    ) -> list[dict[str, object]]:
        # Keep the current request whole. Optional history and run state use only
        # the capacity left after required system instructions and the request.
        current_cost = self._joined_cost(
            [item.content for item in current_segments if item.content]
        )
        required_system_cost = self._joined_cost(
            [item.content for item in system_segments if item.content and item.required]
        )
        reserved_chars = max(0, reserved_chars)
        budget = self.max_chars - reserved_chars
        trailing_segments = trailing_context_segments or []
        trailing_overhead = len(CURRENT_RUN_CONTEXT_PREFIX) + 2 if any(item.content for item in trailing_segments) else 0
        required_trailing_cost = self._joined_cost(
            [item.content for item in trailing_segments if item.content and item.required]
        )
        required_trailing_overhead = trailing_overhead if required_trailing_cost else 0
        if current_cost + required_system_cost + required_trailing_cost + required_trailing_overhead > budget:
            raise ContextBudgetExceededError(
                "Current request and required system instructions exceed the model context budget"
            )

        incoming_groups = observation_groups or []
        groups = [group for group in incoming_groups if self._is_paired_group(group)]
        invalid_groups = len(incoming_groups) - len(groups)
        observation_cost = sum(self._group_cost(group) for group in groups)
        full_system_cost = self._joined_cost([item.content for item in system_segments if item.content])
        full_trailing_cost = self._joined_cost([item.content for item in trailing_segments if item.content])
        full_history, _ = self._select_history(
            conversation, sum(self._message_cost(item) for item in conversation)
        )
        full_history_cost = sum(self._message_cost(item) for item in full_history)
        complete_fits = (
            full_system_cost + current_cost + full_trailing_cost
            + (trailing_overhead if full_trailing_cost else 0)
            + observation_cost + full_history_cost <= budget
        )
        if complete_fits:
            system_limit = full_system_cost
        else:
            # Reserve the newest observation and latest complete dialog from
            # actual remaining capacity, without a fixed system share.
            discretionary = budget - current_cost - required_system_cost - required_trailing_cost - required_trailing_overhead
            newest_cost = sum(self._group_cost(group) for group in groups[self._newest_observation_index(groups):])
            newest_reserve = min(newest_cost, max(0, discretionary))
            latest_pair_cost = (
                sum(self._message_cost(item) for item in full_history[-2:])
                if len(full_history) >= 2 else 0
            )
            pair_reserve = latest_pair_cost if latest_pair_cost <= discretionary - newest_reserve else 0
            system_limit = budget - current_cost - required_trailing_cost - required_trailing_overhead - newest_reserve - pair_reserve
        system, truncated_system = self._pack_segments(system_segments, system_limit, compress_segment)
        stable_count = len(current_segments)
        stable_parts, truncated_current = self._pack_segment_parts(
            current_segments, current_cost
        )
        current = "\n\n".join(
            stable_parts[index] for index in range(stable_count) if index in stable_parts
        )
        remaining_after_system_and_request = max(
            0, budget - len(system) - len(current)
        )
        working_capacity = remaining_after_system_and_request - required_trailing_overhead - required_trailing_cost
        newest_index = self._newest_observation_index(groups)
        newest_cost = sum(self._group_cost(group) for group in groups[newest_index:])
        recent, _ = self._select_history(conversation, working_capacity)
        pair_cost = sum(self._message_cost(item) for item in recent[-2:]) if len(recent) >= 2 else 0
        history_marker_cost = len("[Earlier conversation omitted.]") if len(recent) > 2 else 0
        reserve_pair = (pair_cost + history_marker_cost) if pair_cost and not complete_fits else 0
        omission_marker = "[Earlier current-run observations omitted; inspect a cited source only for a specific missing fact.]"
        if newest_cost + reserve_pair > working_capacity:
            reserve_pair = 0
        marker_reserve = (
            len(omission_marker) if len(groups) > 1
            and observation_cost + reserve_pair > working_capacity
            and newest_cost + reserve_pair + len(omission_marker) <= working_capacity else 0
        )
        recovery_limit = max(
            required_trailing_cost,
            remaining_after_system_and_request - trailing_overhead - observation_cost - reserve_pair - marker_reserve,
        )
        trailing_parts, trailing_truncated = self._pack_segment_parts(
            trailing_segments,
            recovery_limit,
            compress_segment,
        )
        truncated_current = truncated_current or trailing_truncated
        trailing = "\n\n".join(
            trailing_parts[index]
            for index in range(len(trailing_segments))
            if index in trailing_parts
        )
        trailing_message = (
            f"{CURRENT_RUN_CONTEXT_PREFIX}\n\n"
            f"{trailing}"
            if trailing
            else ""
        )
        working_limit = max(
            0,
            budget - len(system) - len(current) - len(trailing_message),
        )
        observation_limit = max(0, working_limit - marker_reserve - reserve_pair)
        observations, omitted_observations, truncated_observations = self._select_observations(groups, observation_limit)
        omitted_observations += invalid_groups
        if omitted_observations and len(omission_marker) <= working_limit - sum(
            self._message_cost(message) for message in observations
        ):
            observations.insert(0, {"role": "user", "content": omission_marker})
        history_limit = working_limit - sum(self._message_cost(item) for item in observations)
        selected, omitted = self._select_history(conversation, history_limit)
        self.last_views = [
            {"observation_id": message["_observation"].identifier,
             "complete": bool(message.get("_complete")),
             "coverage": (message["_observation"].coverage if message.get("_complete")
                          else message.get("_visible_coverage")),
             "reference_visible": bool(message["_observation"].reference)}
            for message in observations if isinstance(message.get("_observation"), Observation)
        ]
        self.last_visibility = frozenset(view["observation_id"] for view in self.last_views if view["complete"])

        included = len(system) + len(current) + len(trailing_message) + sum(
            self._message_cost(item) for item in [*selected, *observations]
        )
        self.last_stats = ContextPackageStats(
            budget_chars=self.max_chars,
            included_chars=included,
            omitted_messages=omitted,
            truncated_system=truncated_system,
            truncated_current=truncated_current,
            omitted_observations=omitted_observations,
            truncated_observations=truncated_observations,
            reserved_chars=reserved_chars,
        )
        messages = [
            {"role": "system", "content": system},
            *selected,
            {"role": "user", "content": current},
        ]
        if trailing:
            messages.append({"role": "user", "content": trailing_message})
        messages.extend({key: value for key, value in message.items() if not key.startswith("_")}
                        for message in observations)
        return messages

    @staticmethod
    def _message_cost(message: dict[str, object]) -> int:
        cost = len(str(message.get("content") or ""))
        if message.get("tool_calls"):
            cost += len(json.dumps(message["tool_calls"], ensure_ascii=False))
        return cost + len(str(message.get("tool_call_id", "")))

    def _group_cost(self, group: list[dict[str, object]]) -> int:
        return sum(self._message_cost(message) for message in group)

    @staticmethod
    def _is_paired_group(group: list[dict[str, object]]) -> bool:
        calls = {call["id"] for message in group for call in message.get("tool_calls", [])}
        returns = {message.get("tool_call_id") for message in group if message.get("role") == "tool"}
        return calls == returns

    def _select_observations(
        self, groups: list[list[dict[str, object]]], limit: int,
    ) -> tuple[list[dict[str, object]], int, int]:
        if not groups:
            return [], 0, 0
        selected = []
        remaining = max(0, limit)
        truncated = 0
        newest_observation = self._newest_observation_index(groups)
        later = range(len(groups) - 1, newest_observation, -1)
        order = (
            [index for index in later if self._is_control_group(groups[index])]
            + [newest_observation]
            + [index for index in later if not self._is_control_group(groups[index])]
            + list(range(newest_observation - 1, -1, -1))
        )
        for index in order:
            group = groups[index]
            # Never send a pending call or orphan return, including cancelled turns.
            if not self._is_paired_group(group):
                continue
            cost = self._group_cost(group)
            if cost <= remaining:
                selected.append((index, group))
                remaining -= cost
            elif index == newest_observation:
                clipped = self._clip_observation(group, remaining)
                if clipped:
                    selected.append((index, clipped))
                    remaining -= self._group_cost(clipped)
                    truncated += int(any(
                        original.get("role") in {"tool", "user"}
                        and original.get("content") != projected.get("content")
                        for original, projected in zip(group, clipped)
                    ))
        selected.sort(key=lambda item: item[0])
        return [message for _, group in selected for message in group], len(groups) - len(selected), truncated

    def _newest_observation_index(self, groups: list[list[dict[str, object]]]) -> int:
        return next((index for index in range(len(groups) - 1, -1, -1)
                     if self._has_observation(groups[index])), max(0, len(groups) - 1))

    @staticmethod
    def _is_control_group(group: list[dict[str, object]]) -> bool:
        return any(isinstance(message.get("_observation"), Observation)
                   and message["_observation"].control for message in group)

    @staticmethod
    def _has_observation(group: list[dict[str, object]]) -> bool:
        for message in group:
            record = message.get("_observation")
            if isinstance(record, Observation):
                if not record.control:
                    return True
                continue
            if message.get("role") == "tool":
                return True
            content = str(message.get("content") or "")
            if message.get("role") == "user" and content.startswith("Observation: "):
                source = json.loads(content.partition("\n")[0].removeprefix("Observation: "))
                if source.get("tool") != "progress_guard":
                    return True
            if content.startswith("Current-run observation"):
                return True
        return False

    def _clip_observation(self, group: list[dict[str, object]], limit: int) -> list[dict[str, object]]:
        marker = "\n[Runtime truncated observation body; inspect the source for the missing content.]"
        clipped = [dict(message) for message in group]
        for message in clipped:
            if message.get("role") == "assistant" and message.get("content") and self._group_cost(clipped) > limit:
                message["content"] = "[Earlier assistant text omitted to preserve the tool result.]"
        if self._group_cost(clipped) <= limit:
            return clipped
        bodies = []
        for message in clipped:
            if message.get("_atomic"):
                continue
            content = str(message.get("content") or "")
            header, separator, body = content.partition("\n")
            # A tool's first line contains its identity, status, range and source.
            if message.get("role") in {"tool", "user"} and separator:
                record = message.get("_observation")
                if isinstance(record, Observation):
                    header = record.header(complete=False)
                    body = str(message.get("_body", body))
                bodies.append((message, body))
                message["content"] = header + marker
        remaining = limit - self._group_cost(clipped)
        if remaining < 0 or not bodies:
            return []
        for index, (message, body) in enumerate(bodies):
            share = min(len(body), remaining // (len(bodies) - index))
            message["content"] = str(message["content"]).removesuffix(marker) + "\n" + body[:max(0, share - 1)] + marker
            message["_complete"] = False
            message["_body"] = body[:max(0, share - 1)]
            record = message.get("_observation")
            if isinstance(record, Observation) and record.coverage is not None:
                start, end = record.coverage
                visible_lines = str(message["_body"]).count("\n")
                message["_visible_coverage"] = (start, min(end, start + visible_lines - 1))
                header = record.header(complete=False, coverage=message["_visible_coverage"])
                _, _, rendered_body = str(message["content"]).partition("\n")
                message["content"] = header + "\n" + rendered_body
            remaining -= share
        return clipped if self._group_cost(clipped) <= limit else []

    @staticmethod
    def _joined_cost(contents: list[str]) -> int:
        return sum(len(content) for content in contents) + max(0, len(contents) - 1) * 2

    def _select_history(
        self,
        conversation: list[dict[str, object]],
        limit: int,
    ) -> tuple[list[dict[str, object]], int]:
        turns: list[list[dict[str, str]]] = []
        pending_user: dict[str, str] | None = None
        for message in conversation:
            role = message.get("role")
            content = message.get("content")
            if role == "user" and isinstance(content, str) and content:
                if pending_user is not None:
                    pending_user = None
                pending_user = {"role": "user", "content": content}
            elif role == "assistant" and isinstance(content, str) and content and pending_user:
                turns.append([pending_user, {"role": "assistant", "content": content}])
                pending_user = None

        marker = "[Earlier conversation omitted.]"
        budget = max(0, limit)
        total_cost = sum(
            len(item["content"]) for turn in turns for item in turn
        )
        if total_cost <= budget:
            return [message for turn in turns for message in turn], 0

        remaining = max(0, budget - len(marker))
        selected_turns: list[list[dict[str, str]]] = []
        for turn in reversed(turns):
            cost = sum(len(item["content"]) for item in turn)
            if cost > remaining:
                break
            selected_turns.append(turn)
            remaining -= cost
        selected_turns.reverse()
        omitted = len(turns) - len(selected_turns)
        selected = [message for turn in selected_turns for message in turn]
        if omitted and len(marker) <= budget:
            selected.insert(0, {"role": "user", "content": marker})
        return selected, omitted * 2

    def _pack_segments(
        self,
        segments: list[ContextSegment],
        limit: int,
        compress_segment: Callable[[ContextSegment, int], str] | None = None,
    ) -> tuple[str, bool]:
        selected, truncated = self._pack_segment_parts(segments, limit, compress_segment)
        return "\n\n".join(selected[index] for index in sorted(selected)), truncated

    def _pack_segment_parts(
        self,
        segments: list[ContextSegment],
        limit: int,
        compress_segment: Callable[[ContextSegment, int], str] | None = None,
    ) -> tuple[dict[int, str], bool]:
        candidates = list(segments)
        required = [
            (index, item)
            for index, item in enumerate(candidates)
            if item.content and item.required
        ]
        required_cost = sum(len(item.content) for _, item in required) + max(0, len(required) - 1) * 2
        if required and required_cost > limit:
            selected: dict[int, str] = {}
            remaining = limit
            for position, (index, segment) in enumerate(required):
                separator = 2 if selected else 0
                available = remaining - separator
                share = available // max(1, len(required) - position)
                clipped = (
                    segment.content
                    if len(segment.content) <= share
                    else self._clip_segment(segment, share)
                )
                if clipped:
                    selected[index] = clipped
                    remaining -= len(clipped) + separator
            return selected, True
        ranked = sorted(
            ((index, item) for index, item in enumerate(candidates) if item.content),
            key=lambda item: (not item[1].required, -item[1].priority, item[0]),
        )
        selected: dict[int, str] = {}
        remaining = limit
        truncated = False
        for index, segment in ranked:
            separator = 2 if selected else 0
            available = remaining - separator
            if available <= 0:
                truncated = True
                continue
            if len(segment.content) <= available:
                selected[index] = segment.content
                remaining -= len(segment.content) + separator
                continue
            truncated = True
            if compress_segment is not None and segment.compressible and not segment.required:
                try:
                    compressed = compress_segment(segment, available)
                except Exception as error:
                    raise ContextBudgetExceededError(
                        f"Model context compression failed for {segment.label}"
                    ) from error
                if not isinstance(compressed, str) or not compressed or len(compressed) > available:
                    raise ContextBudgetExceededError(
                        f"Model context compression did not fit {segment.label}"
                    )
                selected[index] = compressed
                remaining -= len(compressed) + separator
                continue
            if not segment.truncatable and not segment.required:
                continue
            clipped = self._clip_segment(segment, available)
            if clipped:
                selected[index] = clipped
                remaining -= len(clipped) + separator
        return selected, truncated

    def _clip_segment(self, segment: ContextSegment, limit: int) -> str:
        marker = f"\n[Runtime truncated {segment.label}.]\n"
        if limit <= len(marker):
            return ""
        lines = segment.content.splitlines(keepends=True)
        if len(lines) <= 1:
            return self._clip_ends(segment.content, limit)
        available = limit - len(marker)
        head: list[str] = []
        tail: list[str] = []
        head_chars = 0
        tail_chars = 0
        head_limit = int(available * 0.6)
        for line in lines:
            if head_chars + len(line) > head_limit:
                break
            head.append(line)
            head_chars += len(line)
        for line in reversed(lines[len(head):]):
            if head_chars + tail_chars + len(line) > available:
                break
            tail.append(line)
            tail_chars += len(line)
        return "".join(head) + marker + "".join(reversed(tail))

    def _clip_ends(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        marker = "\n[Runtime omitted lower-priority context.]\n"
        if limit <= len(marker):
            return ""
        available = max(0, limit - len(marker))
        head = max(1, int(available * 0.4))
        tail = max(0, available - head)
        return value[:head] + marker + (value[-tail:] if tail else "")
