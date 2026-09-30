from __future__ import annotations


class RuntimeCapabilities:
    """User control of existing capability and policy mechanisms."""

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def engine(self):
        return self.runtime.engine

    def capability_command(self, args: list[str], session=None) -> dict:
        warehouse = getattr(self.engine, "capability_warehouse", None)
        if warehouse is None:
            return {"error": "warehouse unavailable"}
        self.runtime.prepare_session_runtime(session)
        operation = args[0].lower() if args else "list"
        values = args[1:]
        try:
            if operation == "list":
                payload = {"entries": [warehouse.describe_entry(item) for item in warehouse.catalog_entries()]}
            elif operation == "open" and len(values) == 1:
                manifest = warehouse.open_toolbox(values[0])
                payload = {
                    "toolbox_id": manifest.toolbox_id,
                    "state": manifest.state,
                    "items": [
                        {"id": item.id, "name": item.name, "risk": item.risk_level, "description": item.description}
                        for item in manifest.items
                    ],
                }
            elif operation == "status" and len(values) <= 1:
                payload = warehouse.status(values[0] if values else None)
            elif operation == "activate" and values:
                scope = "session"
                capability_ids = []
                for value in values:
                    if value.startswith("--scope="):
                        scope = value.split("=", 1)[1]
                    else:
                        capability_ids.append(value)
                toolbox_ids = {
                    entry.id for entry in warehouse.catalog_entries() if entry.kind == "toolbox"
                }
                selected_toolboxes = [
                    capability_id for capability_id in capability_ids if capability_id in toolbox_ids
                ]
                capability_ids = warehouse.expand_activation_targets(capability_ids)
                records = warehouse.activate(capability_ids, scope=scope, reason="activated by user command")
                payload = {
                    "activated": [record.capability_id for record in records],
                    "toolboxes": selected_toolboxes,
                    "scope": scope,
                }
                self.sync_session_capabilities(session, warehouse)
            elif operation == "release":
                selected_toolboxes = [
                    value for value in values if value in warehouse.active_toolbox_ids()
                ] if values else warehouse.active_toolbox_ids()
                release_ids = warehouse.expand_release_targets(values) if values else None
                payload = {
                    "released": warehouse.release(release_ids, reason="released by user command"),
                    "toolboxes": selected_toolboxes,
                }
                self.sync_session_capabilities(session, warehouse)
            else:
                payload = {"error": "usage: /capabilities [list|open <toolbox>|status [toolbox]|activate <id...> [--scope=turn|run|session]|release [id...]]"}
        except (KeyError, ValueError) as exc:
            payload = {"error": str(exc)}
        return payload

    def activate_skill(self, args: list[str], session=None) -> dict:
        if len(args) != 1:
            return {"error": "usage: /skill [name]"}
        warehouse = getattr(self.engine, "capability_warehouse", None)
        if warehouse is None:
            return {"error": "warehouse unavailable"}
        self.runtime.prepare_session_runtime(session)
        toolbox_id = f"skill:{args[0]}"
        try:
            manifest = warehouse.open_toolbox(toolbox_id)
            capability_ids = [item.id for item in manifest.items]
            records = warehouse.activate(capability_ids, scope="session", reason="activated by /skill")
            self.sync_session_capabilities(session, warehouse)
            payload = {"activated": [record.capability_id for record in records], "scope": "session"}
        except (KeyError, ValueError) as exc:
            payload = {"error": str(exc)}
        return payload

    def sync_session_capabilities(self, session, warehouse):
        if session is not None:
            session.active_capability_ids = warehouse.persisted_capability_ids()
            self.runtime.save_session(session)

    def safety_mode(self, requested=None):
        policy = getattr(getattr(self.engine, "guardrails", None), "policy", None)
        if policy is None:
            raise RuntimeError("Engine policy configuration is not available.")
        if requested is not None:
            if requested not in {"readonly", "confirm", "auto"}:
                raise ValueError(f"Invalid mode '{requested}'. Use readonly, confirm, or auto.")
            policy.mode = requested
        return getattr(policy, "mode", "confirm")
