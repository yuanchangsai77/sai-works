"""Internal observation facts. Public tool and session contracts remain unchanged."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .safety.redaction import redact


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _bounded(value):
    if isinstance(value, dict):
        return {str(key): _bounded(item) for key, item in list(value.items())[:50]}
    if isinstance(value, list):
        return [_bounded(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return f"<omitted {len(value)} chars; sha256:{fingerprint(value)}>"
    return value


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    target: str = ""
    selectors: str = ""
    version: str = ""
    coverage: tuple[int, int] | None = None
    certain: bool = False
    complete: bool = True
    control: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    identifier: str
    request_key: str
    evidence_key: str
    tool: str
    workspace: str
    session_id: str
    target: str
    version: str
    coverage: tuple[int, int] | None
    certain: bool
    complete: bool
    success: bool
    error_code: str | None
    body_hash: str
    summary: str
    reference: str = ""
    control: bool = False
    arguments: str = "{}"

    @classmethod
    def create(
        cls,
        action,
        result,
        workspace: str,
        session_id: str = "",
        spec: ObservationSpec | None = None,
    ):
        spec = spec or ObservationSpec(
            selectors=fingerprint(action.arguments),
            complete=not result.metadata.get("truncated", False),
        )
        safe_body = redact(result.output)
        request = fingerprint([action.name, workspace, action.arguments])
        body_hash = fingerprint(safe_body)
        evidence = fingerprint(
            [
                action.name,
                workspace,
                spec.target,
                spec.selectors,
                spec.version,
                spec.coverage,
                body_hash,
                result.success,
                result.error_code,
            ]
        )
        return cls(
            fingerprint([request, evidence]),
            request,
            evidence,
            action.name,
            workspace,
            session_id,
            spec.target,
            spec.version,
            spec.coverage,
            spec.certain,
            spec.complete,
            result.success,
            result.error_code,
            body_hash,
            " ".join(str(safe_body).split())[:400],
            control=spec.control,
            arguments=json.dumps(
                _bounded(redact(action.arguments)), ensure_ascii=False
            ),
        )

    def payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object):
        if not isinstance(value, dict):
            return None
        try:
            data = dict(value)
            if data.get("coverage") is not None:
                data["coverage"] = tuple(data["coverage"])
            record = cls(**data)
            strings = (
                record.identifier,
                record.request_key,
                record.evidence_key,
                record.tool,
                record.workspace,
                record.session_id,
                record.target,
                record.version,
                record.body_hash,
                record.summary,
                record.reference,
                record.arguments,
            )
            if not all(isinstance(item, str) for item in strings):
                return None
            if not all(
                isinstance(item, bool)
                for item in (
                    record.certain,
                    record.complete,
                    record.success,
                    record.control,
                )
            ):
                return None
            if record.coverage is not None and (
                len(record.coverage) != 2
                or not all(type(item) is int for item in record.coverage)
            ):
                return None
            if not isinstance(json.loads(record.arguments), dict):
                return None
            return record
        except (TypeError, ValueError):
            return None

    def header(self, *, complete: bool, coverage: tuple[int, int] | None = None) -> str:
        source = {
            "observation_id": self.identifier,
            "tool": self.tool,
            "success": self.success,
            "workspace": self.workspace,
            "target": self.target,
            "version": self.version,
            "coverage": self.coverage if coverage is None else coverage,
            "complete": complete,
            "action_arguments": json.loads(self.arguments),
        }
        if self.reference:
            source["result_ref"] = self.reference
        if self.error_code:
            source["error_code"] = self.error_code
        return "Observation: " + json.dumps(source, ensure_ascii=False, sort_keys=True)

    def message(
        self, body: str, *, role: str = "user", complete: bool = False,
        coverage: tuple[int, int] | None = None, **fields
    ) -> dict:
        return {
            "role": role,
            "content": self.header(complete=complete, coverage=coverage) + "\n" + body,
            "_observation": self,
            "_complete": complete,
            "_body": body,
            **fields,
        }


def observation_for(result) -> Observation | None:
    return Observation.from_payload(result.metadata.get("_observation"))


class ObservationArchive:
    """Canonical observation archive alongside the existing run artifacts, not a log parser."""

    def __init__(self, logger):
        self.logger = logger

    def save(
        self, observation: Observation, body: str, arguments: dict | None = None
    ) -> Observation:
        run_dir = getattr(self.logger, "run_dir", None)
        if run_dir is None:
            return observation
        directory = Path(run_dir) / "observations"
        path = directory / (observation.identifier + ".json")
        archived = replace(observation, reference=str(path.resolve()))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                pending = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=directory, delete=False
                    ) as handle:
                        pending = Path(handle.name)
                        json.dump(
                            {
                                "observation": archived.payload(),
                                "body": redact(body),
                                "arguments": redact(arguments or {}),
                            },
                            handle,
                            ensure_ascii=False,
                            default=str,
                        )
                        handle.write("\n")
                    pending.replace(path)
                finally:
                    if pending is not None:
                        pending.unlink(missing_ok=True)
                with (directory / "index.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(archived.payload(), ensure_ascii=False) + "\n"
                    )
            return archived
        except (OSError, ValueError, TypeError):
            return observation

    def recover(
        self, run_ids: list[str], session_id: str, allowed_roots: list[str]
    ) -> list[Observation]:
        if not session_id:
            return []
        base = Path(self.logger.base_dir).resolve()
        roots = [Path(root).resolve() for root in allowed_roots]
        recovered = {}
        for run_id in run_ids[-6:]:
            if (
                not isinstance(run_id, str)
                or Path(run_id).name != run_id
                or run_id in {".", ".."}
            ):
                continue
            directory = (base / run_id / "observations").resolve()
            if not directory.is_relative_to(base):
                continue
            index = directory / "index.jsonl"
            try:
                if (
                    not index.resolve().is_relative_to(directory)
                    or index.stat().st_size > 2_000_000
                ):
                    continue
                lines = index.read_text(encoding="utf-8").splitlines()[-128:]
            except (OSError, ValueError):
                continue
            for line in lines:
                try:
                    record = Observation.from_payload(json.loads(line))
                    if record is None or record.session_id != session_id:
                        continue
                    if len(record.identifier) != 64 or any(
                        c not in "0123456789abcdef" for c in record.identifier
                    ):
                        continue
                    if not any(
                        Path(record.workspace).resolve().is_relative_to(root)
                        for root in roots
                    ):
                        continue
                    if record.target.startswith("/") and not any(
                        Path(record.target).resolve().is_relative_to(root)
                        for root in roots
                    ):
                        continue
                    reference = (directory / (record.identifier + ".json")).resolve()
                    if (
                        not reference.is_relative_to(directory)
                        or not reference.is_file()
                    ):
                        continue
                    payload = json.loads(reference.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        continue
                    stored = Observation.from_payload(payload.get("observation"))
                    if (
                        stored != record
                        or fingerprint(payload.get("body")) != record.body_hash
                    ):
                        continue
                    recovered[record.identifier] = replace(
                        record, reference=str(reference)
                    )
                except (ValueError, TypeError, OSError):
                    continue
        return list(recovered.values())
