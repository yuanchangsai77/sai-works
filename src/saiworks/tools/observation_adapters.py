"""Local tool semantics; the orchestration policy never switches on tool names."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..observations import ObservationSpec, fingerprint


def query_observation(*selectors: str):
    def describe(action, result, context):
        target = result.metadata.get("path") or action.arguments.get("path", ".")
        target = str((Path(context.cwd) / str(target)).resolve())
        query = {
            key: action.arguments.get(
                key, False if key.startswith("include_") else None
            )
            for key in selectors
        }
        return ObservationSpec(
            target=target,
            selectors=fingerprint(query),
            certain=True,
            complete=not result.metadata.get("truncated", False),
        )

    return describe


def file_observation(action, result, context):
    metadata = result.metadata
    target = str(
        (
            Path(context.cwd) / str(metadata.get("path") or action.arguments["path"])
        ).resolve()
    )
    start = metadata.get("start_line", action.arguments.get("start_line", 1))
    end = metadata.get("end_line", start - 1)
    total = metadata.get("total_lines", end)
    expected = min(action.arguments.get("end_line", total), total)
    return ObservationSpec(
        target=target,
        version=str(metadata.get("sha256", "")),
        coverage=(start, end),
        certain=bool(metadata.get("sha256")),
        complete=end >= expected,
    )


def file_observation_valid(observation, action, context):
    if not observation.version or not observation.complete:
        return False
    path = (Path(context.cwd) / action.arguments["path"]).resolve()
    if str(path) != observation.target:
        return False
    # This is a freshness check, not execution of the candidate action.
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == observation.version
    except OSError:
        return False


def revision_observation(action, result, context):
    return ObservationSpec(
        target=str(Path(context.cwd).resolve()),
        selectors=fingerprint({"revision": action.arguments["revision"]}),
        version=str(result.metadata.get("resolved_revision", "")),
        certain=True,
        complete=not result.metadata.get("truncated", False),
    )


def control_observation(action, result, context):
    return ObservationSpec(control=True, certain=True)
