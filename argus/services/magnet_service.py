"""Magnet sets: from a settings file to save-and-restore, and onto the machine.

* A settings file (magnet_sets.py: the consoles' CSV, or the legacy .dat) becomes a
  save-and-restore configuration (each supply's CURRENT_SP and STATE_SP, with their
  readbacks) and a snapshot of the same name holding the FILE's values - not the live
  ones: the machine is not read for them, and not touched.
* A set is then put on the machine the way the consoles do it (magnet_apply.py), all
  at once or as pretune steps: each step moves every supply 1/k of the way from its
  setpoint now to its target, k being the steps left, so the last step lands on it.
  Without outside changes that is the consoles' linear pretune, I0 + i * (I1 - I0) / N.
* "standby" and "on" act on every supply of a configuration: STANDBY at 0 A (an ON
  supply is brought to 0 A before it is switched), or ON with its current untouched.
* The set's PVs can be archived by the Archiver Appliance.

Every target current is checked against its CURRENT_SP control limits (DRVL/DRVH)
when the PV has them, before anything is confirmed.
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from typing import Any

from argus.core.errors import ValidationError
from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.epics.exceptions import EpicsError
from argus.providers.epics.interface import EpicsProvider
from argus.providers.saverestore.exceptions import SaveRestoreError, SaveRestoreQueryError
from argus.providers.saverestore.models import SnrPvEntry
from argus.providers.saverestore.saverestore import SaveRestoreProvider
from argus.services.magnet_apply import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_TOLERANCE,
    DEFAULT_ZERO_TOLERANCE,
    ApplyResult,
    apply_targets,
)
from argus.services.magnet_sets import (
    CURRENT_RB,
    CURRENT_SP,
    FIELDS,
    OFF_STATES,
    STATE_RB,
    STATE_SP,
    MagnetFile,
    MagnetTarget,
    parse_magnet_file,
    split_base,
    target_from_base,
)
from argus.services.saverestore_service import SaveRestoreService

ACTIONS = ("set", "standby", "on")
_CONCURRENCY = 10


class MagnetService:
    def __init__(
        self,
        saverestore_service: SaveRestoreService,
        saverestore: SaveRestoreProvider,
        epics: EpicsProvider,
        archiver: ArchiverApplianceProvider,
        inventory: Any = None,
        knowledge: Any = None,
    ) -> None:
        self._snr_service = saverestore_service
        self._snr = saverestore
        self._epics = epics
        self._archiver = archiver
        self._inventory = inventory
        self._knowledge = knowledge

    # --- reading a file ---------------------------------------------------

    async def read_file(
        self, file_text: str | None = None, repo: str | None = None, path: str | None = None, ref: str | None = None
    ) -> tuple[MagnetFile, str]:
        """The parsed file, and where it came from."""
        if file_text and file_text.strip():
            text, source = file_text, "the file given"
        elif path:
            if self._knowledge is None:
                raise ValidationError("Reading a file from git is not available here; give the file's text.")
            content = await self._knowledge.get_file(repo, path, ref or "HEAD")
            text, source = content.content, f"{content.repo}:{content.path}@{content.ref}"
        else:
            raise ValidationError("Give the magnet file: its text as file_text, or repo and path of it in git.")
        prefixes = await self._prefixes() if not _looks_csv(text) else {}
        parsed = parse_magnet_file(text, prefix_of=lambda name: prefixes.get(name.lower()))
        return parsed, source

    async def _prefixes(self) -> dict[str, str]:
        if self._inventory is None:
            return {}
        listing = await self._inventory.list_inventory()
        return {d.name.lower(): d.iocprefix for d in listing.devices if d.iocprefix}

    # --- what the machine says now ----------------------------------------

    async def live(self, targets: list[MagnetTarget]) -> dict[str, dict[str, Any]]:
        """Each supply's CURRENT_SP/RB and STATE_SP/RB now; None where a PV does not answer."""
        names = [t.pv(f) for t in targets for f in FIELDS]
        values = await self._epics.get_many(names, timeout=8.0)
        by_name = {v.name: (None if v.status == "disconnected" else v.value) for v in values}
        return {t.base: {f: by_name.get(t.pv(f)) for f in FIELDS} for t in targets}

    async def check(self, targets: list[MagnetTarget]) -> dict[str, Any]:
        live = await self.live(targets)
        limits = await self.limit_problems(targets)
        rows = []
        for t in targets:
            now = live[t.base]
            rows.append(
                {
                    "magnet": t.name,
                    "pv_prefix": t.base,
                    "target_state": t.state,
                    "target_current": t.current,
                    "state_now": now[STATE_RB],
                    "current_setpoint_now": now[CURRENT_SP],
                    "current_readback_now": now[CURRENT_RB],
                    "connected": any(v is not None for v in now.values()),
                }
            )
        return {
            "rows": rows,
            "not_connected": [r["pv_prefix"] for r in rows if not r["connected"]],
            "outside_limits": limits,
        }

    async def limit_problems(self, targets: list[MagnetTarget]) -> list[str]:
        """Target currents outside their CURRENT_SP control limits, where the PV has them."""
        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def problem(t: MagnetTarget) -> str | None:
            if t.current is None:
                return None
            async with semaphore:
                try:
                    info = await self._epics.info(t.pv(CURRENT_SP))
                except EpicsError:
                    return None
            low, high = info.control_limits or (None, None)
            if low is None or high is None or not low < high or not all(map(math.isfinite, (low, high))):
                return None
            if not low <= t.current <= high:
                return f"{t.name}: {t.current} A is outside its limits [{low}, {high}] A"
            return None

        found = await asyncio.gather(*(problem(t) for t in targets))
        return [p for p in found if p]

    # --- save-and-restore -------------------------------------------------

    async def plan_set(
        self, name: str, folder: str, targets: list[MagnetTarget], *, create_folders: bool = False
    ) -> dict[str, Any]:
        """Whether the configuration exists (and fits) and the snapshot is free: raises if not.
        With create_folders, a missing folder is to be created ("new_folders")."""
        if not name or not name.strip():
            raise ValidationError("A name is needed for the configuration and its snapshot: ask the user for one.")
        name = name.strip()
        folder_path, new_folders = await self._snr_service.folder_plan(folder, create_folders)
        wanted = _pv_entries(targets)
        plan: dict[str, Any] = {"name": name, "folder": folder, "folder_path": folder_path,
                                "new_folders": new_folders, "path": f"{folder_path}/{name}", "config": None,
                                "pv_list": wanted}
        if new_folders:
            return plan
        folder_id, _ = await self._snr_service.resolve_folder(folder)
        children = await self._snr.get_children(folder_id)
        config = next((c for c in children if c.node_type == "CONFIGURATION" and c.name == name), None)
        plan["config"] = config
        if config is None:
            return plan
        existing = await self._snr.get_configuration(config.unique_id)
        have = {p.pv_name for p in existing.pv_list}
        need = {p.pv_name for p in wanted}
        if have != need:
            missing, extra = sorted(need - have), sorted(have - need)
            raise ValidationError(
                f"The configuration {plan['path']} already exists with other PVs"
                + (f" (it lacks {', '.join(missing[:8])}{'...' if len(missing) > 8 else ''})" if missing else "")
                + (f" (it also has {', '.join(extra[:8])}{'...' if len(extra) > 8 else ''})" if extra else "")
                + ". Ask the user for another name."
            )
        snapshots = await self._snr.get_children(config.unique_id)
        if any(s.node_type == "SNAPSHOT" and s.name == name for s in snapshots):
            raise ValidationError(
                f"{plan['path']} already has a snapshot named {name!r}. Ask the user for another name."
            )
        return plan

    async def create_set(
        self, name: str, folder: str, targets: list[MagnetTarget], *, source: str, comment: str = "",
        create_folders: bool = False,
    ) -> dict[str, Any]:
        plan = await self.plan_set(name, folder, targets, create_folders=create_folders)
        name, path = plan["name"], plan["path"]
        folder_id, _, folders_created = await self._snr_service.folder_for_writing(folder, create_folders)
        config = plan["config"]
        created = config is None
        if created:
            config = await self._snr.create_configuration(
                folder_id, name, plan["pv_list"],
                description=f"Magnet set {name}, created by ARGUS from {source}",
            )
        notes: list[str] = []
        try:
            # Only the form of each value (units, display, an enum's states): the values are the file's.
            live_items = await self._snr.read_live_items(config.unique_id)
        except SaveRestoreError as exc:
            notes.append(f"The save-and-restore service could not read the PVs ({exc}); the snapshot's values "
                         "were built from the file and the PVs' states read by ARGUS.")
            live_items = [_bare_item(pv) for pv in plan["pv_list"]]
        items, more = await self._items_from_file(live_items, targets)
        notes += more
        try:
            node, _ = await self._snr.save_snapshot(
                config.unique_id, name, comment or f"Values of {source}, saved by ARGUS", items
            )
        except SaveRestoreError as exc:
            raise SaveRestoreQueryError(
                f"The configuration {path} (id {config.unique_id}) "
                + ("was created" if created else "exists")
                + f", but the snapshot of the file's values could NOT be saved: {exc}. Do not take a live "
                "snapshot instead (that saves the machine's values, not the file's); tell the user, and retry "
                "this same call once the save-and-restore service works - it reuses the configuration."
            ) from exc
        return {
            "configuration": {"unique_id": config.unique_id, "path": path, "created": created},
            "folders_created": folders_created,
            "snapshot": {"unique_id": node.unique_id, "name": node.name, "owner": node.user_name},
            "magnets": len(targets),
            "pv_count": len(plan["pv_list"]),
            "notes": notes,
            "logbook_suggestion": {
                "title": f"Magnet set {name} saved in save-and-restore",
                "text": (
                    ("Created" if created else "Used the existing")
                    + f" save-and-restore configuration {path} (id {config.unique_id}) for {len(targets)} magnet "
                    f"power supplies, and saved the snapshot {node.name!r} (id {node.unique_id}) with the values of "
                    f"{source}. The machine was not changed."
                ),
            },
        }

    async def _items_from_file(
        self, live_items: list[dict[str, Any]], targets: list[MagnetTarget]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The snapshot's items: the file's current and state for each supply, in the form the
        service reads them in (a Phoebus VType), never the live values. A state the file leaves
        alone keeps its live value."""
        by_base = {t.base: t for t in targets}
        notes: list[str] = []
        stamp = _vtime()
        items = []
        for item in live_items:
            pv = (item.get("configPv") or {}).get("pvName", "")
            parts = split_base(pv)
            target = by_base.get(parts[0]) if parts else None
            live = item.get("value") if isinstance(item.get("value"), dict) else None
            out = {"configPv": item.get("configPv"), "value": live, "readbackValue": None}
            if target is not None and parts and parts[1] == CURRENT_SP and target.current is not None:
                out["value"] = _vdouble(target.current, live, stamp)
            elif target is not None and parts and parts[1] == STATE_SP and target.state is not None:
                labels = list(((live or {}).get("enum") or {}).get("labels") or []) or await self._choices(pv)
                if target.state in labels:
                    out["value"] = _venum(labels.index(target.state), labels, live, stamp)
                else:
                    out["value"] = None
                    notes.append(f"{pv}: its states could not be read ({target.state} not among "
                                 f"{labels or 'none'}), so the snapshot has no value for it.")
            elif live is None:
                notes.append(f"{pv}: not connected, and the file gives no value for it.")
            items.append(out)
        return items, notes

    async def _choices(self, pv: str) -> list[str]:
        try:
            return list((await self._epics.info(pv)).choices or [])
        except EpicsError:
            return []

    async def targets_from_snapshot(self, snapshot: str, configuration: str = "") -> tuple[str, list[MagnetTarget]]:
        node, label = await self._snr_service.resolve_snapshot(snapshot, configuration)
        data = await self._snr.get_snapshot(node.unique_id)
        found: dict[str, dict[str, Any]] = {}
        for item in data.items:
            parts = split_base(item.pv_name)
            if parts and parts[1] in (CURRENT_SP, STATE_SP):
                found.setdefault(parts[0], {})[parts[1]] = item.value
        targets = []
        for base, values in found.items():
            current = values.get(CURRENT_SP)
            state = values.get(STATE_SP)
            targets.append(target_from_base(
                base,
                float(current) if isinstance(current, (int, float)) and not isinstance(current, bool) else None,
                str(state).upper() if isinstance(state, str) else None,
            ))
        if not targets:
            raise SaveRestoreQueryError(f"The snapshot {label} holds no magnet CURRENT_SP/STATE_SP values.")
        return label, targets

    async def magnets_of(self, configuration: str) -> tuple[str, list[str]]:
        config, path = await self._snr_service.resolve_configuration(configuration)
        data = await self._snr.get_configuration(config.unique_id)
        bases = []
        for pv in data.pv_list:
            parts = split_base(pv.pv_name)
            if parts and parts[1] in (CURRENT_SP, STATE_SP) and parts[0] not in bases:
                bases.append(parts[0])
        if not bases:
            raise SaveRestoreQueryError(f"The configuration {path} has no magnet CURRENT_SP/STATE_SP PVs.")
        return path, bases

    # --- putting a set on the machine -------------------------------------

    async def resolve_action(
        self, action: str, snapshot: str = "", configuration: str = "", magnets: list[str] | None = None
    ) -> tuple[str, list[MagnetTarget]]:
        """(what it is, the targets) for set / standby / on, limited to `magnets` if given."""
        if action not in ACTIONS:
            raise ValidationError(f"action is one of {', '.join(ACTIONS)}.")
        if action == "set":
            if not snapshot:
                raise ValidationError("action 'set' needs the snapshot to put the magnets at.")
            label, targets = await self.targets_from_snapshot(snapshot, configuration)
            what = f"the snapshot {label}"
        else:
            if not configuration:
                raise ValidationError(f"action {action!r} needs the configuration whose magnets to act on.")
            path, bases = await self.magnets_of(configuration)
            if action == "standby":
                targets = [target_from_base(b, 0.0, "STANDBY") for b in bases]
                what = f"every magnet of {path} to 0 A and STANDBY"
            else:
                targets = [target_from_base(b, None, "ON") for b in bases]
                what = f"every magnet of {path} ON (currents unchanged)"
        if magnets:
            wanted = {m.strip().lower() for m in magnets}
            targets = [t for t in targets if t.name.lower() in wanted or t.base.lower() in wanted]
            if not targets:
                raise ValidationError(f"None of {', '.join(magnets)} is in {what}.")
            what += f", only {', '.join(t.name for t in targets)}"
        return what, targets

    async def pretune_targets(self, targets: list[MagnetTarget], steps_left: int) -> list[MagnetTarget]:
        """The next pretune step: each supply 1/steps_left of the way from its setpoint now to its target.
        A supply that must end OFF/STANDBY is brought down while ON and switched only on the last step;
        one that must end ON is switched ON on the first."""
        if not isinstance(steps_left, int) or steps_left < 1:
            raise ValidationError("steps_left is a whole number of at least 1 (1 = the last step, onto the set).")
        live = await self.live(targets)
        step = []
        for t in targets:
            now = live[t.base]
            sp = now[CURRENT_SP]
            if not isinstance(sp, (int, float)) or isinstance(sp, bool):
                step.append(t)  # not readable: the apply reports it
                continue
            if t.state in OFF_STATES:
                if steps_left == 1 or str(now[STATE_RB]) != "ON":
                    step.append(t)
                else:
                    step.append(MagnetTarget(t.name, t.prefix, sp + (0.0 - sp) / steps_left, "ON"))
            elif t.current is None:
                step.append(t)
            else:
                nxt = t.current if steps_left == 1 else sp + (t.current - sp) / steps_left
                step.append(MagnetTarget(t.name, t.prefix, nxt, t.state))
        return step

    async def apply(
        self,
        targets: list[MagnetTarget],
        *,
        tolerance: float = DEFAULT_TOLERANCE,
        zero_tolerance: float = DEFAULT_ZERO_TOLERANCE,
        timeout: float = DEFAULT_TIMEOUT_S,
        force_current: bool = False,
        zero_off_setpoints: bool = False,
    ) -> ApplyResult:
        problems = await self.limit_problems(targets)
        if problems:
            raise ValidationError("Nothing was changed: " + "; ".join(problems) + ".")
        return await apply_targets(
            self._epics, targets, tolerance=tolerance, zero_tolerance=zero_tolerance, timeout=timeout,
            force_current=force_current, zero_off_setpoints=zero_off_setpoints,
        )

    # --- archiving --------------------------------------------------------

    async def archive_status(self, pvs: list[str]) -> dict[str, str]:
        return await self._archiver.pv_status(pvs)

    async def archive(self, pvs: list[str]) -> dict[str, Any]:
        before = await self._archiver.pv_status(pvs)
        already = sorted(p for p in pvs if is_archived(before.get(p)))
        todo = [p for p in pvs if p not in already]
        answers = await self._archiver.archive_pvs(todo) if todo else {}
        return {
            "already_archived": already,
            "requested": {p: answers.get(p, "no answer") for p in todo},
            "logbook_suggestion": {
                "title": "PVs added to the archiver",
                "text": (
                    f"Asked the Archiver Appliance to archive {len(todo)} PV(s)"
                    + (f": {', '.join(todo[:20])}{'...' if len(todo) > 20 else ''}" if todo else "")
                    + f". {len(already)} were already archived."
                ),
            },
        }


def set_pvs(targets: list[MagnetTarget], include_readbacks: bool = True) -> list[str]:
    fields = FIELDS if include_readbacks else (CURRENT_SP, STATE_SP)
    return [t.pv(f) for t in targets for f in fields]


def is_archived(status: str | None) -> bool:
    text = (status or "").lower()
    return text.startswith("being archived") or "initial sampling" in text or "appliance assigned" in text


def _bare_item(pv: SnrPvEntry) -> dict[str, Any]:
    return {"configPv": {"pvName": pv.pv_name, "readbackPvName": pv.readback_pv_name, "readOnly": pv.read_only},
            "value": None}


def _pv_entries(targets: list[MagnetTarget]) -> list[SnrPvEntry]:
    return [
        entry
        for t in targets
        for entry in (
            SnrPvEntry(pv_name=t.pv(CURRENT_SP), readback_pv_name=t.pv(CURRENT_RB)),
            SnrPvEntry(pv_name=t.pv(STATE_SP), readback_pv_name=t.pv(STATE_RB)),
        )
    ]


def _looks_csv(text: str) -> bool:
    first = next((line for line in text.splitlines() if line.strip()), "")
    return "," in first and "name" in first.lower()


def _vtime() -> dict[str, int]:
    now = time.time()
    return {"unixSec": int(now), "nanoSec": int((now % 1) * 1e9), "userTag": 0}


_NO_ALARM = {"severity": "NONE", "status": "NONE", "name": "NONE"}
_NO_DISPLAY = {
    "lowAlarm": "NaN", "highAlarm": "NaN", "lowDisplay": "NaN", "highDisplay": "NaN",
    "lowWarning": "NaN", "highWarning": "NaN", "units": "",
}


def _vdouble(value: float, live: dict[str, Any] | None, stamp: dict[str, int]) -> dict[str, Any]:
    """A VDouble in Phoebus's JSON (epics vtype-json): the live one's form, with the file's value."""
    if live and (live.get("type") or {}).get("name") in ("VDouble", "VFloat"):
        vtype = copy.deepcopy(live)
    else:
        vtype = {"type": {"name": "VDouble", "version": 1}, "display": dict(_NO_DISPLAY)}
    vtype.update({"value": float(value), "alarm": dict(_NO_ALARM), "time": dict(stamp)})
    return vtype


def _venum(index: int, labels: list[str], live: dict[str, Any] | None, stamp: dict[str, int]) -> dict[str, Any]:
    if live and (live.get("type") or {}).get("name") == "VEnum":
        vtype = copy.deepcopy(live)
    else:
        vtype = {"type": {"name": "VEnum", "version": 1}, "enum": {"labels": list(labels)}}
    vtype.update({"value": index, "alarm": dict(_NO_ALARM), "time": dict(stamp)})
    return vtype
