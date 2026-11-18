from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..orchestration.ext import ContextLoader
from ..orchestration.session import SessionContext
from ..types import UserRequest

MAX_RULE_BYTES = 32_000
PROJECT_BOUNDARY_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")


@dataclass(slots=True)
class ProjectRule:
    path: str
    content: str
    truncated: bool = False


class ProjectRulesLoader(ContextLoader):
    """Load AGENTS.md files from the workspace path up to the project root."""

    def __init__(self, logger=None, max_bytes: int = MAX_RULE_BYTES) -> None:
        self.logger = logger
        self.max_bytes = max(1, int(max_bytes))

    def load_context(self, request: UserRequest, session: SessionContext) -> None:
        cwd = Path(request.cwd).expanduser().resolve()
        rules = self._load_rules(cwd)
        session.project_rules = rules
        if self.logger is not None and rules:
            self.logger.record(
                "context.project_rules",
                {
                    "files": [
                        {
                            "path": rule.path,
                            "bytes": len(rule.content.encode("utf-8")),
                            "truncated": rule.truncated,
                        }
                        for rule in rules
                    ]
                },
            )

    def refresh_observed_paths(self, request: UserRequest, session: SessionContext) -> None:
        """Refresh scoped rules for authorized, structured file observations."""
        cwd = Path(request.cwd).expanduser().resolve()
        roots = [cwd, *(Path(path).resolve() for path in session.workspace_state.approved_roots)]
        directories = {cwd}
        for observation in session.observations:
            if not observation.success or not observation.certain or not observation.target:
                continue
            target = Path(observation.target)
            if not target.is_absolute():
                continue
            target = target.resolve()
            if not any(target.is_relative_to(root) for root in roots):
                continue
            directories.add(target if target.is_dir() else target.parent)
        rules = {}
        for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
            if directory == cwd:
                for rule in self._load_rules(cwd):
                    rules[rule.path] = rule
                continue
            root = max((root for root in roots if directory.is_relative_to(root)), key=lambda item: len(item.parts))
            boundary = root
            for ancestor in (directory, *directory.parents):
                if any((ancestor / marker).exists() for marker in PROJECT_BOUNDARY_MARKERS):
                    boundary = ancestor
                    break
                if ancestor == root:
                    break
            for rule in self._load_rules(directory, boundary=boundary):
                rules[rule.path] = rule
        session.project_rules = list(rules.values())

    def _load_rules(self, cwd: Path, *, boundary: Path | None = None) -> list[ProjectRule]:
        boundary = boundary or self._project_boundary(cwd)
        candidates = []
        for directory in (cwd, *cwd.parents):
            path = directory / "AGENTS.md"
            if path.is_file():
                candidates.append(path)
            if directory == boundary:
                break

        rules = []
        for path in reversed(candidates):
            content, truncated = self._read_text(path)
            if content.strip():
                rules.append(ProjectRule(path=str(path), content=content, truncated=truncated))
        return rules

    def _read_text(self, path: Path) -> tuple[str, bool]:
        data = path.read_bytes()
        truncated = len(data) > self.max_bytes
        chunk = data[: self.max_bytes]
        return chunk.decode("utf-8", errors="replace"), truncated

    def _project_boundary(self, cwd: Path) -> Path:
        for directory in (cwd, *cwd.parents):
            if any((directory / marker).exists() for marker in PROJECT_BOUNDARY_MARKERS):
                return directory
        return cwd
