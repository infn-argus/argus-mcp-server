"""Save-and-restore: browsing, plus creating configurations and snapshots.

Kept as its own service (rather than folded into e.g. HistoryService) since
save-and-restore is its own domain (configurations/snapshots), matching how
knowledge_service and beamline_inventory_service each own one domain.

Creating a configuration from devices: each device's setpoint PVs, taken from
its device-type profile's key PVs (services/device_types/: a "<x>_setpoint"
role, paired with its "<x>_readback" when readbacks are wanted), so a magnet
contributes CURRENT_SP (+CURRENT_RB) and STATE_SP (+STATE_RB). A device whose
type has no setpoint convention (a vacuum gauge, a motor - no profile yet) is
reported as skipped, never silently dropped; explicit PVs cover those.

A snapshot always belongs to an existing configuration: the service itself
reads the configuration's PVs and ARGUS saves the result.

Restoring writes a snapshot's setpoints back to the machine (the service
does the writing; readbacks and read-only PVs are never written). Its
preview is the service's own comparison: which PVs would change, from what
to what.

All three report what was done in a form ready for the logbook
(logbook_suggestion) - the tool asks the user whether to log it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from argus.core.errors import ValidationError
from argus.providers.saverestore.exceptions import SaveRestoreQueryError
from argus.providers.saverestore.models import SnrConfiguration, SnrNode, SnrPvEntry, SnrSnapshot
from argus.providers.saverestore.saverestore import SaveRestoreProvider

_SETPOINT = "_setpoint"
_READBACK = "_readback"


@dataclass
class ConfigurationPlan:
    """The PVs a configuration would get, and why some devices contribute none."""

    pv_list: list[SnrPvEntry] = field(default_factory=list)
    devices: dict[str, list[str]] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)


class SaveRestoreService:
    def __init__(self, saverestore: SaveRestoreProvider, inventory: Any = None) -> None:
        self._saverestore = saverestore
        # BeamlineInventoryService or KnowledgeHubInventoryService: whichever
        # source this server reads devices from.
        self._inventory = inventory

    async def search(self, query: str) -> list[SnrNode]:
        return await self._saverestore.search(query)

    async def get_node(self, node_id: str) -> SnrNode:
        return await self._saverestore.get_node(node_id)

    async def get_children(self, node_id: str) -> list[SnrNode]:
        return await self._saverestore.get_children(node_id)

    async def get_configuration(self, config_id: str) -> SnrConfiguration:
        return await self._saverestore.get_configuration(config_id)

    async def get_snapshot(self, snapshot_id: str) -> SnrSnapshot:
        return await self._saverestore.get_snapshot(snapshot_id)

    # --- creating -------------------------------------------------------

    async def resolve_folder(self, folder: str) -> tuple[str, str]:
        return await self._saverestore.resolve_folder(folder)

    async def plan_configuration(
        self,
        devices: list[str],
        *,
        include_readbacks: bool = True,
        pvs: list[dict[str, Any]] | None = None,
    ) -> ConfigurationPlan:
        plan = ConfigurationPlan()
        seen: set[str] = set()

        def add(setpoint: str, readback: str | None, owner: str) -> None:
            if setpoint in seen:
                return
            seen.add(setpoint)
            plan.pv_list.append(SnrPvEntry(pv_name=setpoint, readback_pv_name=readback if include_readbacks else None))
            plan.devices.setdefault(owner, []).append(setpoint)

        if devices:
            if self._inventory is None:
                raise ValidationError("No device inventory here: give the PVs explicitly with pvs.")
            listing = await self._inventory.list_inventory()
            by_name = {d.name.lower(): d for d in listing.devices}
            for wanted in devices:
                device = by_name.get(str(wanted).strip().lower())
                if device is None:
                    plan.skipped[wanted] = "not in this beamline's device inventory"
                    continue
                setpoints = sorted(r for r in device.key_pvs if r.endswith(_SETPOINT))
                if not setpoints:
                    plan.skipped[device.name] = (
                        f"no setpoint convention for its type (devgroup {device.devgroup or 'unknown'}) - "
                        "give its PVs explicitly with pvs"
                    )
                    continue
                for role in setpoints:
                    readback = device.key_pvs.get(role[: -len(_SETPOINT)] + _READBACK)
                    add(device.key_pvs[role], readback, device.name)

        for item in pvs or []:
            if isinstance(item, str):
                setpoint, readback = item, None
            elif isinstance(item, dict) and item.get("setpoint"):
                setpoint, readback = str(item["setpoint"]), item.get("readback")
            else:
                raise ValidationError('Each pvs item is a PV name or {"setpoint": ..., "readback": ...}.')
            add(setpoint.strip(), str(readback).strip() if readback else None, "(explicit PVs)")

        return plan

    async def create_configuration(
        self,
        name: str,
        folder: str,
        devices: list[str],
        *,
        include_readbacks: bool = True,
        pvs: list[dict[str, Any]] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        plan = await self.plan_configuration(devices, include_readbacks=include_readbacks, pvs=pvs)
        if not plan.pv_list:
            skipped = "; ".join(f"{d}: {why}" for d, why in plan.skipped.items()) or "no devices or PVs given"
            raise ValidationError(f"Nothing to save: {skipped}.")
        folder_id, folder_path = await self._saverestore.resolve_folder(folder)
        node = await self._saverestore.create_configuration(
            folder_id, name, plan.pv_list, description=description or _describe_devices(plan)
        )
        path = f"{folder_path}/{node.name}"
        readbacks = sum(1 for pv in plan.pv_list if pv.readback_pv_name)
        return {
            "configuration": {"unique_id": node.unique_id, "name": node.name, "path": path, "owner": node.user_name},
            "pv_count": len(plan.pv_list),
            "readback_count": readbacks,
            "devices": plan.devices,
            "skipped_devices": plan.skipped,
            "logbook_suggestion": {
                "title": f"Save-and-restore configuration {node.name} created",
                "text": (
                    f"Created the save-and-restore configuration {path} (id {node.unique_id}) with "
                    f"{len(plan.pv_list)} setpoint PV(s)"
                    + (f" and {readbacks} readback(s)" if readbacks else "")
                    + f", for: {', '.join(d for d in plan.devices)}."
                    + (f" Not included: {', '.join(plan.skipped)}." if plan.skipped else "")
                ),
            },
        }

    async def resolve_configuration(self, configuration: str) -> tuple[SnrNode, str]:
        """A configuration given by node id or by path ("BTF_CONF/Magnets/Quads")."""
        text = (configuration or "").strip()
        if _is_uuid(text):
            node = await self._saverestore.get_node(text)
            if node.node_type != "CONFIGURATION":
                raise SaveRestoreQueryError(f"{text} is a {node.node_type}, not a configuration.")
            return node, node.name
        folder, _, name = text.strip("/").rpartition("/")
        if not folder or not name:
            raise ValidationError("configuration is a node id or a path such as BTF_CONF/Quadrupoles.")
        folder_id, folder_path = await self._saverestore.resolve_folder(folder)
        children = await self._saverestore.get_children(folder_id)
        configs = [c for c in children if c.node_type == "CONFIGURATION"]
        node = next((c for c in configs if c.name == name), None) or next(
            (c for c in configs if c.name.lower() == name.lower()), None
        )
        if node is None:
            names = ", ".join(sorted(c.name for c in configs)) or "none"
            raise SaveRestoreQueryError(f"No configuration {name!r} in {folder_path}. Configurations there: {names}.")
        return node, f"{folder_path}/{node.name}"

    async def take_snapshot(self, configuration: str, *, name: str = "", comment: str = "") -> dict[str, Any]:
        config, config_path = await self.resolve_configuration(configuration)
        when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        snapshot_name = name.strip() or f"{config.name} {when}"
        node, items = await self._saverestore.take_snapshot(config.unique_id, snapshot_name, comment or snapshot_name)
        missing = [i.pv_name for i in items if i.value is None]
        return {
            "snapshot": {"unique_id": node.unique_id, "name": node.name, "owner": node.user_name},
            "configuration": {"unique_id": config.unique_id, "path": config_path},
            "pv_count": len(items),
            "not_connected": missing,
            "logbook_suggestion": {
                "title": f"Save-and-restore snapshot {node.name}",
                "text": (
                    f"Took the save-and-restore snapshot {node.name!r} (id {node.unique_id}) of configuration "
                    f"{config_path} (id {config.unique_id}): {len(items)} PV(s) saved"
                    + (f", {len(missing)} not connected ({', '.join(missing[:10])})" if missing else "")
                    + "."
                    + (f" Comment: {comment}" if comment else "")
                ),
            },
        }


    # --- snapshots of a configuration, and restoring one -----------------

    async def list_snapshots(self, configuration: str) -> tuple[SnrNode, str, list[SnrNode]]:
        """A configuration's snapshots, newest first."""
        config, path = await self.resolve_configuration(configuration)
        children = await self._saverestore.get_children(config.unique_id)
        snapshots = [c for c in children if c.node_type in ("SNAPSHOT", "COMPOSITE_SNAPSHOT")]
        epoch = datetime.min.replace(tzinfo=timezone.utc)
        snapshots.sort(key=lambda n: _aware(n.created) or epoch, reverse=True)
        return config, path, snapshots

    async def resolve_snapshot(self, snapshot: str, configuration: str = "") -> tuple[SnrNode, str]:
        """(snapshot node, "<configuration path> / <snapshot name>") from an id, or a name or
        "latest" within a configuration."""
        text = (snapshot or "").strip()
        if _is_uuid(text):
            node = await self._saverestore.get_node(text)
            if node.node_type not in ("SNAPSHOT", "COMPOSITE_SNAPSHOT"):
                raise SaveRestoreQueryError(f"{text} is a {node.node_type}, not a snapshot.")
            return node, node.name
        if not configuration:
            raise ValidationError(
                "Give the snapshot's id, or its configuration (path or id) with the snapshot's name or 'latest'."
            )
        _, path, snapshots = await self.list_snapshots(configuration)
        if not snapshots:
            raise SaveRestoreQueryError(f"The configuration {path} has no snapshots.")
        if text.lower() in ("", "latest", "last", "newest"):
            return snapshots[0], f"{path} / {snapshots[0].name}"
        node = next((n for n in snapshots if n.name == text), None) or next(
            (n for n in snapshots if n.name.lower() == text.lower()), None
        )
        if node is None:
            names = ", ".join(n.name for n in snapshots[:15])
            raise SaveRestoreQueryError(f"No snapshot {text!r} of {path}. Its snapshots, newest first: {names}.")
        return node, f"{path} / {node.name}"

    async def restore_preview(self, snapshot: str, configuration: str = "") -> dict[str, Any]:
        node, label = await self.resolve_snapshot(snapshot, configuration)
        compared = await self._saverestore.compare(node.unique_id)
        changes = [c for c in compared if not c["equal"]]
        return {"snapshot": node, "label": label, "changes": changes, "unchanged": len(compared) - len(changes)}

    async def restore(self, snapshot: str, configuration: str = "") -> dict[str, Any]:
        preview = await self.restore_preview(snapshot, configuration)
        node, label = preview["snapshot"], preview["label"]
        failures = await self._saverestore.restore(node.unique_id)
        written = len(preview["changes"]) + preview["unchanged"] - len(failures)
        changed = ", ".join(f"{c['pv_name']} {c['live']} -> {c['saved']}" for c in preview["changes"][:15])
        return {
            "snapshot": {"unique_id": node.unique_id, "name": node.name, "label": label},
            "pvs_written": max(written, 0),
            "changed_before_restore": preview["changes"],
            "failed": failures,
            "logbook_suggestion": {
                "title": f"Save-and-restore: restored snapshot {node.name}",
                "text": (
                    f"Restored the save-and-restore snapshot {label} (id {node.unique_id}): "
                    f"{max(written, 0)} setpoint PV(s) written"
                    + (f"; changed from live values: {changed}" if changed else "; no PV differed from the snapshot")
                    + (
                        f". {len(failures)} PV(s) FAILED: "
                        + ", ".join(f"{f['pv_name']} ({f['error']})" for f in failures[:10])
                        if failures
                        else ""
                    )
                    + "."
                ),
            },
        }


def _is_uuid(text: str) -> bool:
    return len(text) == 36 and text.count("-") == 4 and "/" not in text


def _aware(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)

def _describe_devices(plan: ConfigurationPlan) -> str:
    return "Created by ARGUS for: " + ", ".join(plan.devices)
