from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True, slots=True)
class ContextSegment:
    content: str
    label: str
    priority: int = 50
    required: bool = False
    truncatable: bool = True


class ContextPackager:
    """Build a bounded prompt while preserving control-plane facts first."""

    def __init__(self, max_chars: int = 120_000) -> None:
        self.max_chars = max(4_000, int(max_chars))
        self.last_stats = ContextPackageStats(self.max_chars, 0)

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
    ) -> list[dict[str, object]]:
        # Keep the current request whole. Optional history and run state use only
        # the capacity left after required system instructions and the request.
        current_cost = self._joined_cost(
            [item.content for item in current_segments if item.content]
        )
        required_system_cost = self._joined_cost(
            [item.content for item in system_segments if item.content and item.required]
        )
        if current_cost + required_system_cost > self.max_chars:
            raise ContextBudgetExceededError(
                "Current request and required system instructions exceed the model context budget"
            )

        system_target = max(2_000, int(self.max_chars * 0.50))
        system_limit = min(
            max(system_target, required_system_cost),
            self.max_chars - current_cost,
        )
        system, truncated_system = self._pack_segments(system_segments, system_limit)
        stable_count = len(current_segments)
        stable_parts, truncated_current = self._pack_segment_parts(
            current_segments, current_cost
        )
        current = "\n\n".join(
            stable_parts[index] for index in range(stable_count) if index in stable_parts
        )
        trailing_segments = trailing_context_segments or []
        has_trailing_segments = any(item.content for item in trailing_segments)
        trailing_overhead = len(CURRENT_RUN_CONTEXT_PREFIX) + 2 if has_trailing_segments else 0
        remaining_after_system_and_request = max(
            0, self.max_chars - len(system) - len(current)
        )
        trailing_parts, trailing_truncated = self._pack_segment_parts(
            trailing_segments,
            min(
                int(self.max_chars * 0.10),
                max(0, remaining_after_system_and_request - trailing_overhead),
            ),
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
        history_limit = max(
            0,
            self.max_chars - len(system) - len(current) - len(trailing_message),
        )
        selected, omitted = self._select_history(conversation, history_limit)

        included = len(system) + len(current) + len(trailing_message) + sum(
            len(str(item.get("content", ""))) for item in selected
        )
        self.last_stats = ContextPackageStats(
            budget_chars=self.max_chars,
            included_chars=included,
            omitted_messages=omitted,
            truncated_system=truncated_system,
            truncated_current=truncated_current,
        )
        messages = [
            {"role": "system", "content": system},
            *selected,
            {"role": "user", "content": current},
        ]
        if trailing:
            messages.append({"role": "user", "content": trailing_message})
        return messages

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
        if omitted:
            selected.insert(0, {"role": "user", "content": marker})
        return selected, omitted * 2

    def _pack_segments(
        self,
        segments: list[ContextSegment],
        limit: int,
    ) -> tuple[str, bool]:
        selected, truncated = self._pack_segment_parts(segments, limit)
        return "\n\n".join(selected[index] for index in sorted(selected)), truncated

    def _pack_segment_parts(
        self,
        segments: list[ContextSegment],
        limit: int,
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
        available = max(0, limit - len(marker))
        head = max(1, int(available * 0.4))
        tail = max(0, available - head)
        return value[:head] + marker + (value[-tail:] if tail else "")
