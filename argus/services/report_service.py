"""The beamline description the daily report works from, and the archiving it needs.

Each beamline repo carries deploy/argus-report.yaml: rules saying how to read and judge
each class of device (magnets, motors, cameras, vacuum...), plus what the values files do
not know (key PVs, trips, paths, modes, experiments). The device inventory itself stays in
the values files; this module resolves the rules against them, the same way the
ChannelFinder feeder reads them (iocDefaults merged by template, softiocs included,
disabled IOCs left out, {pv} = iocprefix[:iocroot]:device).

The report computes its facts from archived history, so every PV it reads must be in the
Archiver Appliance: coverage() lists those that are not, and archive_missing() asks the
appliance to archive the ones that answer.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from argus.core.errors import ArgusError
from argus.services.magnet_service import is_archived

REPORT_CONFIG_PATH = "deploy/argus-report.yaml"
DEFAULT_VALUES_FILES = ["deploy/values.yaml"]
# A PV pattern matching more names than this is a config mistake, not a set to archive.
MAX_PATTERN_MATCHES = 2000


class ReportConfigError(ArgusError):
    code = "report_config_error"


@dataclass
class Device:
    name: str
    pv: str
    ioc: str
    devgroup: str | None
    devtype: str | None
    zones: list[str]
    prefix: str


@dataclass
class ResolvedDevice:
    device: Device
    group: str
    function: str | None
    paths: list[str]
    # role ("readback", "state", ...) -> PV name, or a pattern when the role has * or ?
    fields: dict[str, str]


@dataclass
class Resolution:
    beamline: str
    devices: list[ResolvedDevice]
    key_pvs: list[str]
    key_patterns: list[str]
    no_function: list[str] = field(default_factory=list)
    no_path: list[str] = field(default_factory=list)
    no_interface: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.devices:
            out[d.group] = out.get(d.group, 0) + 1
        return out


@dataclass
class Coverage:
    required: list[str]
    archived: list[str]
    not_archived: list[str]
    not_answering: list[str]
    empty_patterns: list[str]

    def to_dict(self, limit: int = 50) -> dict[str, Any]:
        def some(names: list[str]) -> dict[str, Any]:
            return {"count": len(names), "names": names[:limit], **({"more": len(names) - limit} if len(names) > limit else {})}

        return {
            "required": len(self.required),
            "archived": len(self.archived),
            "to_archive": some(self.not_archived),
            "not_answering": some(self.not_answering),
            **({"patterns_matching_nothing": self.empty_patterns} if self.empty_patterns else {}),
        }


# --- values files -> devices ---------------------------------------------------


def _merge_values(base: dict, override: dict) -> dict:
    """Helm's merge: maps combine recursively, later files win."""
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _merge_values(base[key], value)
        else:
            base[key] = value
    return base


def _entries(iocs: Any) -> list[tuple[str, dict]]:
    if isinstance(iocs, dict):
        return [((e.get("name") or k), e) for k, e in iocs.items() if isinstance(e, dict)]
    if isinstance(iocs, list):
        return [(e["name"], e) for e in iocs if isinstance(e, dict) and e.get("name")]
    return []


def _disabled(entry: dict) -> bool:
    return bool(entry.get("disable") or entry.get("disabled")) or entry.get("enabled") is False


def _zones(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(z).strip() for z in items if str(z).strip()]


def inventory(values_texts: list[str]) -> list[Device]:
    """The devices the values files deploy, in the order the files list them."""
    data: dict = {}
    for text in values_texts:
        try:
            _merge_values(data, yaml.safe_load(text) or {})
        except yaml.YAMLError as exc:
            raise ReportConfigError(f"A values file is not valid YAML: {exc}") from exc
    defaults = data.get("iocDefaults") or {}
    iocs = _entries((data.get("epicsConfiguration") or {}).get("iocs"))
    soft_defaults = data.get("softiocDefaults") or {}
    names = {n for n, _ in iocs}
    iocs += [(n, {**soft_defaults, **e}) for n, e in _entries(data.get("softiocs")) if n not in names]

    devices: list[Device] = []
    for name, raw in iocs:
        if _disabled(raw):
            continue
        entry = dict(defaults.get(raw.get("template") or raw.get("devtype") or "", {}) or {})
        entry.update(raw)
        prefix = str(entry.get("iocprefix") or "")
        root = entry.get("iocroot")
        base = f"{prefix}:{root}" if prefix and root else prefix
        devtype = entry.get("devtype") or entry.get("template")
        ioc_zones = _zones(entry.get("zones"))
        listed = [d if isinstance(d, dict) else {"name": str(d)} for d in entry.get("devices") or []]
        for dev in listed:
            dev_name = dev.get("name")
            if not dev_name or not base:
                continue
            devices.append(Device(name=dev_name, pv=f"{base}:{dev_name}", ioc=name, devgroup=entry.get("devgroup"),
                                  devtype=dev.get("devtype") or devtype, zones=_zones(dev.get("zones")) or ioc_zones,
                                  prefix=prefix))
        if not listed and base:
            devices.append(Device(name=name, pv=base, ioc=name, devgroup=entry.get("devgroup"), devtype=devtype,
                                  zones=ioc_zones, prefix=prefix))
    return devices


# --- config rules -> resolved devices --------------------------------------------


def _selected(device: Device, select: dict) -> bool:
    if "devgroup" in select and device.devgroup != select["devgroup"]:
        return False
    if "devtype" in select and device.devtype != select["devtype"]:
        return False
    if device.devtype in (select.get("exclude_devtype") or []):
        return False
    if set(device.zones) & set(select.get("exclude_zones") or []):
        return False
    if any(device.prefix.startswith(p) for p in select.get("exclude_prefix") or []):
        return False
    if "zones" in select and not set(device.zones) & set(select["zones"]):
        return False
    return True


def _is_pattern(name: str) -> bool:
    return "*" in name or "?" in name


def _strings(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return [node] if isinstance(node, str) else []


def resolve(config: dict, devices: list[Device]) -> Resolution:
    interfaces = config.get("interfaces") or {}
    naming = re.compile((config.get("naming") or {}).get("pattern") or r"^$")
    paths = config.get("paths") or {}
    resolved: list[ResolvedDevice] = []
    no_function: list[str] = []
    no_interface: list[str] = []
    no_path: list[str] = []
    seen: set[str] = set()

    for group, rules in (config.get("groups") or {}).items():
        if not isinstance(rules, dict) or "select" not in rules:
            continue  # a derived group (e.g. pointing) works on other groups' devices
        for device in devices:
            if device.pv in seen or not _selected(device, rules["select"]):
                continue
            seen.add(device.pv)
            match = naming.match(device.name)
            parts = match.groupdict() if match else {}
            function = None
            if rules.get("function_from"):
                function = (rules.get("function_overrides") or {}).get(device.name) or \
                    (rules.get("functions") or {}).get(parts.get(rules["function_from"]))
                if function is None:
                    no_function.append(device.name)
            iface_name = rules.get("interface") or (rules.get("interface_by_devtype") or {}).get(device.devtype)
            iface = interfaces.get(iface_name) if iface_name else None
            if iface is None:
                no_interface.append(device.name)
                iface = {}
            section = parts.get("section") or parts.get("station")
            on = [p for p, v in paths.items() if isinstance(v, dict) and (
                (section and section in (v.get("sections") or []))
                or set(device.zones) & set(v.get("zones") or [])
                or device.name in (v.get("devices") or []))]
            if not on:
                no_path.append(device.name)
            resolved.append(ResolvedDevice(device=device, group=group, function=function, paths=on,
                                           fields={role: str(t).replace("{pv}", device.pv) for role, t in iface.items()}))

    named = [s for s in _strings({k: config.get(k) for k in ("key_pvs", "trips")}) if ":" in s and "{" not in s]
    return Resolution(
        beamline=str(config.get("beamline") or ""),
        devices=resolved,
        key_pvs=list(dict.fromkeys(s for s in named if not _is_pattern(s))),
        key_patterns=list(dict.fromkeys(s for s in named if _is_pattern(s))),
        no_function=no_function, no_path=no_path, no_interface=no_interface,
    )


async def required_pvs(resolution: Resolution,
                       expand: Callable[[str, int], Awaitable[tuple[list[str], bool]]]) -> tuple[list[str], list[str]]:
    """Every PV the report reads, patterns expanded through ChannelFinder; and the patterns that matched nothing."""
    exact: list[str] = []
    patterns: list[str] = list(resolution.key_patterns)
    for d in resolution.devices:
        for name in d.fields.values():
            (patterns if _is_pattern(name) else exact).append(name)  # a motor's .RBV archives as is
    exact += resolution.key_pvs
    unique_patterns = list(dict.fromkeys(patterns))
    results = await asyncio.gather(*(expand(p, MAX_PATTERN_MATCHES) for p in unique_patterns))
    empty: list[str] = []
    for pattern, (names, more) in zip(unique_patterns, results):
        if more:
            raise ReportConfigError(f"'{pattern}' matches more than {MAX_PATTERN_MATCHES} PVs: narrow it in "
                                    f"{REPORT_CONFIG_PATH}.")
        if not names:
            empty.append(pattern)
        exact += names
    return list(dict.fromkeys(exact)), empty


class ReportService:
    def __init__(self, knowledge: Any, operations: Any, archiver: Any) -> None:
        self._knowledge = knowledge
        self._operations = operations
        self._archiver = archiver

    async def load(self, repo: str | None = None, ref: str | None = None) -> tuple[dict, list[Device]]:
        try:
            file = await self._knowledge.get_file(repo, REPORT_CONFIG_PATH, ref)
        except ArgusError as exc:
            raise ReportConfigError(f"No {REPORT_CONFIG_PATH} in the beamline repo ({exc}).") from exc
        try:
            config = yaml.safe_load(file.content) or {}
        except yaml.YAMLError as exc:
            raise ReportConfigError(f"{REPORT_CONFIG_PATH} is not valid YAML: {exc}") from exc
        if not isinstance(config, dict):
            raise ReportConfigError(f"{REPORT_CONFIG_PATH} must be a mapping.")
        files = config.get("values_files") or DEFAULT_VALUES_FILES
        texts = [(await self._knowledge.get_file(repo, path, ref)).content for path in files]
        return config, inventory(texts)

    async def resolution(self, repo: str | None = None, ref: str | None = None) -> Resolution:
        config, devices = await self.load(repo, ref)
        return resolve(config, devices)

    async def coverage(self, repo: str | None = None, ref: str | None = None) -> Coverage:
        resolution = await self.resolution(repo, ref)
        required, empty = await required_pvs(resolution, self._operations.pv_names_matching)
        status = await self._archiver.pv_status(required)
        archived = [p for p in required if is_archived(status.get(p))]
        missing = [p for p in required if not is_archived(status.get(p))]
        values = await self._operations.get_pvs(missing, timeout=3.0) if missing else []
        silent = {v.name for v in values if v.status == "disconnected"}
        return Coverage(required=required, archived=archived,
                        not_archived=[p for p in missing if p not in silent],
                        not_answering=[p for p in missing if p in silent], empty_patterns=empty)

    async def archive_missing(self, repo: str | None = None, ref: str | None = None) -> dict[str, Any]:
        """Ask the appliance to archive every PV the report needs that answers and is not archived yet.
        PVs that do not answer are left out: the appliance would wait for them forever."""
        cov = await self.coverage(repo, ref)
        answers = await self._archiver.archive_pvs(cov.not_archived) if cov.not_archived else {}
        refused = {pv: a for pv, a in answers.items() if "submitted" not in a.lower() and not is_archived(a)}
        return {**cov.to_dict(), "requested": len(cov.not_archived),
                **({"not_accepted": refused} if refused else {})}
