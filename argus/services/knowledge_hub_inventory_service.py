"""Beamline device inventory read from the ARGUS Knowledge Hub instead of the deploy YAML.

The hub's EPIK8s import (argus-knowledge-hub's services/epik8s_import.py)
turns every device of a beamline's values.yaml into a "Control Device"
record, with iocDefaults already merged the same way
BeamlineInventoryService merges them:

    system     -> devgroup          function -> devfunc (when the YAML set one)
    devtype    -> devtype           zones    -> zones
    ioc        -> IOC name          pv_prefix-> iocprefix[:iocroot]:device
    geo, inventory_url (-> asset)

So both sources answer list_beamline_devices with the same BeamlineDevice
shape, and the same question can be put to a YAML-backed and a hub-backed
ARGUS to compare them. Differences that remain are real differences
between the sources: the hub names a device by its `name` (the YAML source
prefers its `alias`), it reflects whichever revision was last imported
rather than the branch HEAD, and people may have corrected records in it.

Paging: search_objects(types=["Control Device"], with_attributes=true,
offset=...) pages of at most 100. A hub that predates `offset` and
`with_attributes` silently ignores both (it drops unknown arguments); this
copes by fetching attributes per object with get_object, and stops paging
at the first page that repeats -- noting that the listing is truncated
rather than returning a short list as if it were complete.
"""

from __future__ import annotations

import asyncio
from typing import Any

from argus.providers.knowledge_hub.client import KnowledgeHubClient
from argus.services.beamline_inventory_service import (
    BeamlineDevice,
    InventoryListing,
    matches_filters,
)
from argus.services.device_types import get_profile

CONTROL_DEVICE_TYPE = "Control Device"
PAGE_SIZE = 100
# 50 pages of 100: far beyond any beamline today (BTF is a few hundred).
MAX_PAGES = 50
_CONCURRENCY = 8


class KnowledgeHubInventoryService:
    def __init__(self, client: KnowledgeHubClient) -> None:
        self._client = client

    async def list_inventory(
        self,
        repo: str | None = None,
        path: str | None = None,
        ref: str | None = None,
        devgroup: str | None = None,
        devfunc: str | None = None,
        zone: str | None = None,
        workspace: str | None = None,
    ) -> InventoryListing:
        # repo/path/ref address a git file; the hub's records already say
        # which beamline and revision they came from.
        notes: list[str] = []
        if repo or (path and path != "deploy/values.yaml") or (ref and ref != "HEAD"):
            notes.append(
                "repo/path/ref were ignored: this server reads the device inventory from the ARGUS Knowledge "
                "Hub (the last imported revision of each beamline's configuration), not from git."
            )

        workspaces = await self._client.workspace_ids(only=workspace)
        results, errors = await self._client.for_each_workspace(workspaces, self._workspace_devices)
        for ws, error in errors.items():
            notes.append(f"Workspace {ws!r} could not be read: {error}")

        devices: list[BeamlineDevice] = []
        seen: set[str] = set()
        for ws in workspaces:
            ws_devices, ws_notes = results.get(ws, ([], []))
            notes.extend(ws_notes)
            for uid, device in ws_devices:
                # A record shared across workspaces is visible from each of them; list it once.
                if uid in seen:
                    continue
                seen.add(uid)
                if matches_filters(device, devgroup, devfunc, zone):
                    devices.append(device)

        return InventoryListing(source="knowledge_hub", devices=devices, notes=notes)

    async def _workspace_devices(self, workspace: str) -> tuple[list[tuple[str, BeamlineDevice]], list[str]]:
        objects, notes = await self._all_control_devices(workspace)
        missing = [o for o in objects if not isinstance(o.get("attributes"), dict)]
        if missing:
            attributes = await self._fetch_attributes(workspace, [o["uid"] for o in missing])
            for o in missing:
                o["attributes"] = attributes.get(o["uid"], {})
        return [(o["uid"], _to_device(o, workspace)) for o in objects], notes

    async def _all_control_devices(self, workspace: str) -> tuple[list[dict[str, Any]], list[str]]:
        objects: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        for page in range(MAX_PAGES):
            payload = await self._client.call_tool(
                "search_objects",
                {
                    "types": [CONTROL_DEVICE_TYPE],
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                    "with_attributes": True,
                },
                workspace=workspace,
            )
            if not isinstance(payload, dict):
                break
            if payload.get("unknown_types"):
                return [], [
                    f"Workspace {workspace!r} has no {CONTROL_DEVICE_TYPE!r} type: its EPIK8s configuration "
                    "has not been imported into the hub."
                ]
            total = int(payload.get("total") or 0)
            fresh = [
                o for o in payload.get("objects") or [] if isinstance(o, dict) and o.get("uid") and o["uid"] not in seen
            ]
            if not fresh:
                break
            for o in fresh:
                seen.add(o["uid"])
                objects.append(o)
            if len(objects) >= total:
                break

        notes: list[str] = []
        if len(objects) < total:
            notes.append(
                f"Workspace {workspace!r}: only {len(objects)} of {total} control devices could be listed "
                "(the hub does not support paging search_objects with `offset`; upgrade the hub)."
            )
        return objects, notes

    async def _fetch_attributes(self, workspace: str, uids: list[str]) -> dict[str, dict[str, Any]]:
        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def fetch(uid: str) -> tuple[str, dict[str, Any]]:
            async with semaphore:
                obj = await self._client.call_tool("get_object", {"uid_or_key": uid}, workspace=workspace)
            attributes = obj.get("attributes") if isinstance(obj, dict) else None
            return uid, attributes if isinstance(attributes, dict) else {}

        return dict(await asyncio.gather(*(fetch(uid) for uid in uids)))


def _zones(value: Any) -> list[str]:
    if not value:
        return ["ALL"]
    if isinstance(value, str):
        return [value]
    return [str(z) for z in value]


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _iocprefix(pv_prefix: str | None, name: str) -> str | None:
    if not pv_prefix:
        return None
    suffix = f":{name}"
    return pv_prefix[: -len(suffix)] if pv_prefix.endswith(suffix) else pv_prefix


def _to_device(obj: dict[str, Any], workspace: str) -> BeamlineDevice:
    attributes: dict[str, Any] = obj.get("attributes") or {}
    name = str(obj.get("name") or "")
    devgroup = attributes.get("system") or None
    pv_prefix = attributes.get("pv_prefix") or attributes.get("pv") or None
    iocprefix = _iocprefix(pv_prefix, name)
    profile = get_profile(devgroup)
    devfunc = attributes.get("function") or (
        profile.derive_devfunc(name, iocprefix or "") if profile else None
    )
    return BeamlineDevice(
        name=name,
        ioc_name=str(attributes.get("ioc") or ""),
        iocprefix=iocprefix,
        devgroup=devgroup,
        devtype=attributes.get("devtype") or None,
        devfunc=devfunc,
        zones=_zones(attributes.get("zones")),
        geo=_float(attributes.get("geo")),
        asset=attributes.get("inventory_url") or None,
        key_pvs=profile.key_pvs_for_prefix(pv_prefix) if profile else {},
        workspace=workspace,
        link=obj.get("link") or obj.get("url"),
    )
