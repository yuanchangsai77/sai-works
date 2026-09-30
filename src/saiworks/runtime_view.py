from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace


@dataclass(frozen=True, slots=True)
class CapabilityView:
    entries: tuple
    active_toolboxes: tuple[str, ...]
    descriptions: dict

    def catalog_entries(self):
        return list(deepcopy(self.entries))

    def active_toolbox_ids(self):
        return list(self.active_toolboxes)

    def describe_entry(self, entry):
        return deepcopy(self.descriptions[entry.id])


def build_view(engine):
    """Detached display data in the shape consumed by the existing presenters.

    No executable model/tool, credential, policy or mutable warehouse is exposed.
    This is a local compatibility projection, not the revision snapshot protocol.
    """
    if engine is None:
        return None
    warehouse = getattr(engine, "capability_warehouse", None)
    capabilities = None
    if warehouse is not None:
        entries = warehouse.catalog_entries()
        capabilities = CapabilityView(tuple(deepcopy(entries)), tuple(warehouse.active_toolbox_ids()),
                                      {entry.id: deepcopy(warehouse.describe_entry(entry)) for entry in entries})
    policy = getattr(getattr(engine, "guardrails", None), "policy", None)
    return SimpleNamespace(
        model=SimpleNamespace(model=getattr(getattr(engine, "model", None), "model", "StubModel")),
        guardrails=SimpleNamespace(policy=SimpleNamespace(mode=getattr(policy, "mode", "confirm"))),
        tools=SimpleNamespace(_tools={name: None for name in getattr(getattr(engine, "tools", None), "_tools", {})}),
        context_loaders=tuple(None for _ in getattr(engine, "context_loaders", [])),
        capability_warehouse=capabilities,
    )
