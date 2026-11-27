"""Device resolution: the metadata-only half of the device-centric model.

ChannelFinder is the source of truth (see docs/adr/0002). A `device` property
on CF channels groups PVs under one device name; other well-known properties
(deviceClass, iocName, rack, owner, k8sNamespace, k8sPod, argocdApp, gitRepo,
docRef) populate the rest of the Device model, all optional. If ChannelFinder
is unconfigured, resolve_device() still works for a bare PV name — it just
can't enrich it with cross-system metadata.

This service only assembles metadata; it never fetches live data (PVs, pod
status, logs) — that's diagnostics_service's job, keeping "providers
retrieve, services combine" honest even at the service-to-service boundary.
"""

from __future__ import annotations

import re

from argus.core.cache import AsyncTTLCache
from argus.core.errors import ArgusError
from argus.providers.channelfinder.channelfinder import ChannelFinderProvider
from argus.providers.channelfinder.exceptions import ChannelFinderError
from argus.providers.channelfinder.models import CFChannel
from argus.services.models import Device

_DEVICE_PROPERTY = "device"
_KNOWN_PROPERTIES = {
    "deviceClass": "device_class",
    "iocName": "ioc_name",
    "rack": "rack",
    "owner": "owner",
    "k8sNamespace": "namespace",
    "k8sPod": "pod_name",
    "argocdApp": "argocd_app",
    "gitRepo": "git_repo",
    "docRef": "documentation_refs",
    "networkLocation": "network_location",
}


_POD_ORDINAL_RE = re.compile(r"-\d+$")
# Preferred "headline" PV of a device: its main readback, not whichever PV
# ChannelFinder happened to list first.
_PRIMARY_SUFFIXES = (
    ":CURRENT_RB", ":ArrayRate_RBV", ":STATE_RB", ":STATUS", ":Acquire_RBV", ":PRESSURE_RB", ":VALUE_RB",
)


class DeviceNotFoundError(ArgusError):
    code = "device_not_found"


def _looks_like_pv(name: str) -> bool:
    return ":" in name and " " not in name


def _primary_pv(pv_names: list[str]) -> str | None:
    for suffix in _PRIMARY_SUFFIXES:
        for pv in pv_names:
            if pv.endswith(suffix):
                return pv
    readbacks = [pv for pv in pv_names if pv.endswith(("_RB", "_RBV"))]
    return (readbacks or pv_names or [None])[0]


class DeviceService:
    def __init__(self, channelfinder: ChannelFinderProvider, cache: AsyncTTLCache) -> None:
        self._channelfinder = channelfinder
        self._cache = cache

    async def resolve_device(self, name: str) -> Device:
        return await self._cache.get_or_set(f"device:{name}", lambda: self._resolve_device_uncached(name))

    async def _resolve_device_uncached(self, name: str) -> Device:
        """Find the device a name means, in order: the ChannelFinder `device`
        property itself (EUAPS:CAM:FI1-CAM-01); a device whose property ends with
        the name (FI1-CAM-01); an IOC name (`iocName`, also as its pod's name
        fi1-cam-vis-01-0), when that IOC serves one device; a PV name. Anything
        else is an error naming what to try - never a made-up PV, which only
        times out and reads as "the device is down"."""
        if not self._channelfinder.is_configured():
            if _looks_like_pv(name):
                return Device(name=name, pv_names=[name], primary_pv=name)
            raise DeviceNotFoundError(
                f"{name!r} is not a PV name, and ChannelFinder (which maps device names to PVs) is not "
                "configured here. Pass a full PV name, or find the device with list_beamline_devices."
            )

        by_device = await self._find_grouped(**{_DEVICE_PROPERTY: name})
        if not by_device and ":" not in name:
            by_device = await self._find_grouped(**{_DEVICE_PROPERTY: f"*:{name}"})
        if len(by_device) == 1:
            [(device_name, channels)] = by_device.items()
            return _build_device(device_name, channels)
        if len(by_device) > 1:
            raise DeviceNotFoundError(
                f"{name!r} matches several devices: {', '.join(sorted(by_device)[:20])}. Use the full name."
            )

        ioc_devices = await self._ioc_devices(name)
        if ioc_devices:
            ioc, devices = ioc_devices
            if len(devices) == 1:
                [(device_name, channels)] = devices.items()
                return _build_device(device_name, channels)
            raise DeviceNotFoundError(
                f"{name!r} is IOC {ioc!r}, not a device. It serves {len(devices)} devices: "
                f"{', '.join(sorted(devices)[:30])}. Ask about one of them."
            )

        if _looks_like_pv(name):
            try:
                channel = await self._channelfinder.get_channel(name)
            except ChannelFinderError:
                channel = None
            # A PV ChannelFinder doesn't list may still exist: read it as given.
            return _build_device(name, [channel]) if channel else Device(name=name, pv_names=[name], primary_pv=name)

        raise DeviceNotFoundError(
            f"No device, IOC or PV named {name!r} in ChannelFinder. Device names look like "
            "<prefix>:<device> (e.g. EUAPS:CAM:FI1-CAM-01) or just the device part (FI1-CAM-01); find them "
            "with list_beamline_devices or search_pvs."
        )

    async def _find_grouped(self, **properties: str) -> dict[str, list[CFChannel]]:
        try:
            channels = await self._channelfinder.find_channels(**properties)
        except ChannelFinderError:
            return {}
        grouped: dict[str, list[CFChannel]] = {}
        for channel in channels:
            device = channel.properties.get(_DEVICE_PROPERTY)
            if device:
                grouped.setdefault(device, []).append(channel)
        return grouped

    async def _ioc_devices(self, name: str) -> tuple[str, dict[str, list[CFChannel]]] | None:
        # A pod name is the IOC name plus a StatefulSet ordinal: fi1-cam-vis-01-0.
        candidates = [name]
        stripped = _POD_ORDINAL_RE.sub("", name)
        if stripped != name:
            candidates.append(stripped)
        for candidate in candidates:
            devices = await self._find_grouped(iocName=candidate)
            if devices:
                return candidate, devices
        return None

    async def search_devices(self, query: str, limit: int = 50) -> list[Device]:
        if not self._channelfinder.is_configured():
            return []
        try:
            channels = await self._channelfinder.find_channels(query)
        except ChannelFinderError:
            return []

        by_device: dict[str, list[CFChannel]] = {}
        for channel in channels[:limit]:
            device_name = channel.properties.get(_DEVICE_PROPERTY, channel.name)
            by_device.setdefault(device_name, []).append(channel)

        return [_build_device(name, chans) for name, chans in by_device.items()]


def _build_device(name: str, channels: list[CFChannel]) -> Device:
    pv_names = [c.name for c in channels]
    merged_properties: dict[str, str] = {}
    tags: set[str] = set()
    owner: str | None = None
    for channel in channels:
        merged_properties.update(channel.properties)
        tags.update(channel.tags)
        owner = owner or channel.owner

    device = Device(
        name=name,
        pv_names=pv_names,
        primary_pv=_primary_pv(pv_names),
        owner=owner,
        tags=sorted(tags),
        properties=merged_properties,
    )
    for cf_key, field_name in _KNOWN_PROPERTIES.items():
        value = merged_properties.get(cf_key)
        if value is None:
            continue
        if field_name == "documentation_refs":
            device.documentation_refs = [value]
        else:
            setattr(device, field_name, value)
    return device
