"""Structured device inventory parsed from an epik8s-style beamline deploy YAML.

epik8s beamline repos (e.g. epik8s-btf's deploy/values.yaml) declare devices
under `epicsConfiguration.iocs`, classified by `devgroup` (mag/vac/mot/cam/
diag/...) and `devtype`. Critically, many IOC entries don't set `devgroup`/
`devtype` directly — they inherit them from `iocDefaults[template]`, keyed by
the IOC's own `template` (or `devtype` as a fallback key). Reading the raw
YAML text without applying this merge undercounts devices, since
template-inherited classification is invisible without it.

This mirrors the merge-then-classify logic already used by the Phoebus OPI
console's own config loader (epik8s-btf/opi/epik8s-opi/Scripts/epik8sutil.py:
_merge_ioc_defaults + the devgroup/devfunc rules in conf_to_dev), so ARGUS
and the control-room OPIs agree on what counts as e.g. a quadrupole.

Per-devgroup classification and key-PV-suffix conventions (what counts as a
quadrupole, which PV suffix holds a magnet's current readback, ...) live in
services/device_types/ -- a small per-device-type module each, not one big
table here, so adding/enriching a device type never touches this parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from argus.core.errors import ArgusError
from argus.services.device_types import get_profile
from argus.services.knowledge_service import KnowledgeService

DEFAULT_INVENTORY_PATH = "deploy/values.yaml"


class BeamlineInventoryError(ArgusError):
    code = "beamline_inventory_error"


@dataclass
class BeamlineDevice:
    name: str
    ioc_name: str
    iocprefix: str | None
    devgroup: str | None
    devtype: str | None
    devfunc: str | None
    zones: list[str]
    geo: float | None = None
    asset: str | None = None
    opi: str | None = None
    template: str | None = None
    # role name (e.g. "current_readback") -> full PV name, from the
    # device's devgroup profile (services/device_types/). Empty if the
    # devgroup has no registered profile or key PVs yet.
    key_pvs: dict[str, str] = field(default_factory=dict)
    # Knowledge Hub source only (None from the YAML): the workspace the
    # record was read in and its ready-made Markdown link to the record's
    # page in the hub. list_beamline_devices drops them when None, so the
    # YAML source's output is unchanged.
    workspace: str | None = None
    link: str | None = None


@dataclass
class InventoryListing:
    """What list_beamline_devices returns, whichever source answered."""

    source: str
    devices: list[BeamlineDevice]
    # Anything the caller should know the listing may be missing (a
    # workspace that failed, a hub too old to page through every device).
    notes: list[str] = field(default_factory=list)


def matches_filters(device: BeamlineDevice, devgroup: str | None, devfunc: str | None, zone: str | None) -> bool:
    if devgroup and device.devgroup != devgroup:
        return False
    if devfunc and device.devfunc != devfunc:
        return False
    if zone and zone not in device.zones and "ALL" not in device.zones:
        return False
    return True


def _merge_ioc_defaults(ioc_defaults: dict, ioc: dict) -> dict:
    template = ioc.get("template") or ioc.get("devtype") or ""
    defaults = ioc_defaults.get(template) if template else None
    if not defaults:
        return ioc
    merged = dict(defaults)
    merged.update(ioc)
    return merged


def _normalize_zones(value: object) -> list[str]:
    if value is None:
        return ["ALL"]
    if isinstance(value, str):
        return [value]
    return [str(z) for z in value]


def _device_zones(ioc: dict, dev: dict) -> list[str]:
    ioc_zone_raw = ioc.get("zones", "ALL")
    if "zones" in dev:
        zones = _normalize_zones(dev["zones"])
        if isinstance(ioc_zone_raw, str) and ioc_zone_raw != "ALL" and ioc_zone_raw not in zones:
            zones.append(ioc_zone_raw)
        return zones
    return _normalize_zones(ioc_zone_raw)


class BeamlineInventoryService:
    def __init__(self, knowledge: KnowledgeService) -> None:
        self._knowledge = knowledge

    async def list_devices(
        self,
        repo: str | None,
        path: str = DEFAULT_INVENTORY_PATH,
        ref: str = "HEAD",
        devgroup: str | None = None,
        devfunc: str | None = None,
        zone: str | None = None,
    ) -> list[BeamlineDevice]:
        file = await self._knowledge.get_file(repo, path, ref)
        try:
            data = yaml.safe_load(file.content) or {}
        except yaml.YAMLError as exc:
            raise BeamlineInventoryError(f"Could not parse '{path}' as YAML: {exc}") from exc

        ioc_defaults = data.get("iocDefaults") or {}
        iocs = ((data.get("epicsConfiguration") or {}).get("iocs")) or []
        # epik8s deploy YAMLs key iocs by IOC name (a dict), not a list - every
        # real beamline repo (epik8s-btf, epik8-sparc, epik8s-euaps) uses this
        # form, unlike the list form used in this module's own tests. Iterating
        # a dict directly yields its string keys, not the IOC mappings, which
        # crashed _merge_ioc_defaults()'s `ioc.get(...)` with an unhandled
        # AttributeError on every real deployment.
        if isinstance(iocs, dict):
            iocs = list(iocs.values())

        devices: list[BeamlineDevice] = []
        for raw_ioc in iocs:
            ioc = _merge_ioc_defaults(ioc_defaults, raw_ioc)
            ioc_name = ioc.get("name", "")
            iocprefix = ioc.get("iocprefix", "")
            ioc_devgroup = ioc.get("devgroup")
            ioc_devtype = ioc.get("devtype")
            ioc_asset = ioc.get("asset")
            ioc_opi = ioc.get("opi")
            template = ioc.get("template")

            profile = get_profile(ioc_devgroup)

            for dev in ioc.get("devices", []):
                name = dev.get("alias") or dev.get("name")
                if not name:
                    continue
                dev_devtype = dev.get("devtype", ioc_devtype)
                dev_devfunc = dev.get("devfunc") or (
                    profile.derive_devfunc(name, iocprefix) if profile else None
                )

                device = BeamlineDevice(
                    name=name,
                    ioc_name=ioc_name,
                    iocprefix=iocprefix or None,
                    devgroup=ioc_devgroup,
                    devtype=dev_devtype,
                    devfunc=dev_devfunc,
                    zones=_device_zones(ioc, dev),
                    geo=dev.get("geo"),
                    asset=dev.get("asset", ioc_asset),
                    opi=dev.get("opi", ioc_opi),
                    template=template,
                    key_pvs=profile.key_pvs(iocprefix, name) if profile else {},
                )

                if matches_filters(device, devgroup, devfunc, zone):
                    devices.append(device)

        return devices

    async def list_inventory(
        self,
        repo: str | None,
        path: str = DEFAULT_INVENTORY_PATH,
        ref: str = "HEAD",
        devgroup: str | None = None,
        devfunc: str | None = None,
        zone: str | None = None,
        workspace: str | None = None,
    ) -> InventoryListing:
        # workspace is the Knowledge Hub source's argument; a YAML has none.
        devices = await self.list_devices(repo, path=path, ref=ref, devgroup=devgroup, devfunc=devfunc, zone=zone)
        return InventoryListing(source="yaml", devices=devices)
