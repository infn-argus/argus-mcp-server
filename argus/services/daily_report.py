"""The daily operations report: facts computed from archived history, rendered as a logbook entry.

Code measures, the model explains: every number here is computed from the Archiver
Appliance for the PVs deploy/argus-report.yaml names (see report_service.py). The facts
are kept as JSON next to the rendered Markdown, so a later summary written by a model can
be checked against them.

Two kinds of PV are read differently:
* discrete (states, setpoints, motor done/limit flags, acquiring, alarms, interlocks):
  raw samples - every change, with its time, and the value at the start and end;
* continuous (readbacks, pressures, rates, centroids...): 5-minute bins from the appliance
  (mean, min, max), so a day of a 1 Hz PV is ~288 points.
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from argus.services.report_service import Resolution, ResolvedDevice

# Roles read raw, change by change; every other role is read in bins.
DISCRETE_ROLES = {
    "state", "state_setpoint", "setpoint", "done_moving", "high_limit", "low_limit", "acquiring",
    "detector_state", "status", "alarm", "warning", "gap_setpoint", "high_voltage", "overlays",
    "interlocks", "alarms", "errors",
}
BINS_PER_DAY = 288
MAX_ROWS = 25
CONCURRENCY = 16


@dataclass
class Discrete:
    first: Any = None
    last: Any = None
    changes: list[tuple[str, Any]] = field(default_factory=list)   # (ISO time, new value) inside the window
    # fraction of the window spent at each value (as text)
    share: dict[str, float] = field(default_factory=dict)


@dataclass
class Continuous:
    bins: int = 0
    first: float | None = None
    last: float | None = None
    mean: float | None = None
    median: float | None = None
    min: float | None = None
    max: float | None = None
    stdev_of_bins: float | None = None
    units: str | None = None


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _key(v: Any) -> str:
    n = _num(v)
    return str(int(n)) if n is not None and n == int(n) else str(v)


def summarize_discrete(samples: list, start: datetime, end: datetime) -> Discrete:
    """Changes inside [start, end) and the share of the window at each value. The last sample
    before start gives the value the window started with."""
    out = Discrete()
    samples = sorted(samples, key=lambda s: s.timestamp)
    before = [s for s in samples if s.timestamp < start]
    inside = [s for s in samples if start <= s.timestamp < end]
    if not before and not inside:
        return out
    current = before[-1].value if before else None
    t = start
    durations: dict[str, float] = {}
    for s in inside:
        if current is None:          # nothing known before the first sample: no share, no change
            current, t = s.value, s.timestamp
            continue
        if s.value == current:
            continue
        durations[_key(current)] = durations.get(_key(current), 0.0) + (s.timestamp - t).total_seconds()
        out.changes.append((_iso(s.timestamp), s.value))
        current, t = s.value, s.timestamp
    durations[_key(current)] = durations.get(_key(current), 0.0) + (end - t).total_seconds()
    out.first = before[-1].value if before else inside[0].value
    out.last = current
    total = (end - start).total_seconds() or 1.0
    out.share = {k: round(v / total, 4) for k, v in durations.items()}
    return out


def summarize_continuous(samples: list) -> Continuous:
    means = [m for m in (_num(s.value) for s in samples) if m is not None]
    if not means:
        return Continuous()
    lows = [x for x in (_num(s.min) if s.min is not None else _num(s.value) for s in samples) if x is not None]
    highs = [x for x in (_num(s.max) if s.max is not None else _num(s.value) for s in samples) if x is not None]
    return Continuous(bins=len(means), first=means[0], last=means[-1], mean=statistics.fmean(means),
                      median=statistics.median(means), min=min(lows), max=max(highs),
                      stdev_of_bins=statistics.pstdev(means) if len(means) > 1 else 0.0)


class DailyReport:
    """Reads the history of every PV the description needs and computes the day's facts."""

    def __init__(self, archiver: Any, expand: Any, logbook: Any | None = None) -> None:
        self._archiver = archiver
        self._expand = expand
        self._logbook = logbook

    async def _series(self, pv: str, discrete: bool, start: datetime, end: datetime) -> Discrete | Continuous | None:
        try:
            if discrete:
                # from a little earlier, to know the value the day started with
                series = await self._archiver.get_data(pv, start - timedelta(days=7), end, timeout=20.0)
                return summarize_discrete(series.samples, start, end)
            series = await self._archiver.get_data(pv, start, end, max_points=BINS_PER_DAY, timeout=20.0)
            summary = summarize_continuous(series.samples)
            summary.units = series.units
            return summary
        except Exception:  # noqa: BLE001 - a PV without history is a data gap, reported, not fatal
            return None

    async def facts(self, config: dict, resolution: Resolution, start: datetime, end: datetime) -> dict[str, Any]:
        sem = asyncio.Semaphore(CONCURRENCY)
        reads: dict[str, bool] = {}
        for d in resolution.devices:
            for role, pv in d.fields.items():
                if "*" not in pv and "?" not in pv:
                    reads[pv] = role in DISCRETE_ROLES
        key = (config.get("key_pvs") or {})
        trips = config.get("trips") or []
        trip_pvs: dict[str, list[str]] = {}
        for trip in trips:
            names: list[str] = []
            for watch in trip.get("watch") or []:
                if "{" in watch:
                    continue
                if "*" in watch or "?" in watch:
                    matched, _ = await self._expand(watch, 2000)
                    names += matched
                else:
                    names.append(watch)
            trip_pvs[trip.get("name", "trip")] = names
            for n in names:
                reads[n] = True
        beam = key.get("beam_on") if isinstance(key.get("beam_on"), dict) else None
        if beam:
            reads[beam["pv"]] = False

        async def read(pv: str, discrete: bool):
            async with sem:
                return pv, await self._series(pv, discrete, start, end)

        results = dict(await asyncio.gather(*(read(pv, disc) for pv, disc in reads.items())))
        gaps = sorted(pv for pv, s in results.items() if s is None or (isinstance(s, Continuous) and not s.bins)
                      or (isinstance(s, Discrete) and s.first is None and not s.changes))

        facts: dict[str, Any] = {
            "beamline": resolution.beamline, "start": _iso(start), "end": _iso(end),
            "pvs_read": len(reads), "pvs_without_history": len(gaps), "gaps": gaps,
            "groups": {}, "trips": {}, "beam": None, "logbook": [],
        }
        for group, rules in (config.get("groups") or {}).items():
            members = [d for d in resolution.devices if d.group == group]
            if members:
                facts["groups"][group] = self._group_facts(members, rules or {}, results)
        for name, names in trip_pvs.items():
            events = []
            for pv in names:
                s = results.get(pv)
                if isinstance(s, Discrete):
                    events += [{"pv": pv, "time": t, "value": v} for t, v in s.changes if _num(v) not in (0.0, None)]
            facts["trips"][name] = sorted(events, key=lambda e: e["time"])
        if beam:
            s = results.get(beam["pv"])
            if isinstance(s, Continuous) and s.bins:
                facts["beam"] = {"pv": beam["pv"], "above": beam.get("above"), "mean": s.mean, "max": s.max,
                                 "bins_with_data": s.bins}
        if pointing := (config.get("groups") or {}).get("pointing"):
            facts["groups"]["pointing"] = self._pointing(resolution, pointing, results)
        if self._logbook is not None:
            try:
                facts["logbook"] = await self._entries(start, end)
            except Exception as exc:  # noqa: BLE001 - said in the report, not fatal
                facts["logbook_error"] = str(exc)[:300]
        return facts

    def _group_facts(self, members: list[ResolvedDevice], rules: dict, results: dict) -> dict[str, Any]:
        rows = []
        for d in members:
            row: dict[str, Any] = {"device": d.device.name, "function": d.function, "paths": d.paths}
            for role, pv in d.fields.items():
                s = results.get(pv)
                if s is not None:
                    row[role] = asdict(s)
            rows.append(row)
        out: dict[str, Any] = {"devices": len(members), "rows": rows}
        # flags the renderer lists; computed here so the facts say what was flagged and why
        tol = (rules.get("checks") or {}).get("readback_vs_setpoint")
        flags: list[dict[str, Any]] = []
        for row in rows:
            for role in ("state", "status", "detector_state"):
                if row.get(role, {}).get("changes"):
                    flags.append({"device": row["device"], "kind": f"{role} changes",
                                  "detail": row[role]["changes"][-5:]})
            sp, rb = row.get("setpoint"), row.get("readback")
            # A supply that is off reads ~0 A whatever its setpoint: compare only while it is ON.
            state_now = _num((row.get("state") or {}).get("last"))
            is_on = state_now is None or state_now == (tol or {}).get("on_state", 1)
            if tol and is_on and sp and rb and _num(sp.get("last")) is not None and rb.get("last") is not None:
                diff = abs(rb["last"] - _num(sp["last"]))
                if diff > max(tol.get("abs", 0), tol.get("rel", 0) * abs(_num(sp["last"]))):
                    flags.append({"device": row["device"], "kind": "readback off setpoint",
                                  "detail": {"setpoint": _num(sp["last"]), "readback": rb["last"]}})
            # Screens and flags rest on a limit switch when in or out: only hitting one during the window counts.
            for role in ("high_limit", "low_limit"):
                hits = [t for t, v in (row.get(role) or {}).get("changes", []) if _num(v) == 1]
                if hits:
                    flags.append({"device": row["device"], "kind": f"{role.replace('_', ' ')} switch hit",
                                  "detail": hits[-5:]})
            if (dm := row.get("done_moving")) and _num(dm.get("last")) == 0:
                flags.append({"device": row["device"], "kind": "still moving at end of window"})
            pres = row.get("pressure")
            spikes = (rules.get("checks") or {}).get("spikes")
            if spikes and pres and pres.get("median") and pres.get("max") and \
                    pres["max"] > spikes.get("factor_over_baseline", 10) * pres["median"]:
                flags.append({"device": row["device"], "kind": "pressure spike",
                              "detail": {"max": pres["max"], "median": pres["median"]}})
        out["flags"] = flags
        return out

    @staticmethod
    def _pointing(resolution: Resolution, rules: dict, results: dict) -> dict[str, Any]:
        """Per camera: when the laser was on it (total above a share of its median), and how much the
        centroid moved then - bin means, so seconds-scale jitter is not counted."""
        frac = ((rules.get("checks") or {}).get("laser_present") or {}).get("above_fraction_of_median", 0.2)
        rows = []
        for d in resolution.devices:
            if d.group != "cameras":
                continue
            total = results.get(d.fields.get("total", ""))
            cx = results.get(d.fields.get("centroid_x", ""))
            cy = results.get(d.fields.get("centroid_y", ""))
            if not isinstance(total, Continuous) or not total.bins or not total.median:
                continue
            if not any(isinstance(c, Continuous) and c.bins for c in (cx, cy)):
                continue
            rows.append({"camera": d.device.name, "zones": d.device.zones,
                         "laser_present_threshold": frac * total.median,
                         "centroid_x": asdict(cx) if isinstance(cx, Continuous) else None,
                         "centroid_y": asdict(cy) if isinstance(cy, Continuous) else None})
        return {"cameras": rows}

    async def _entries(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        entries = await self._logbook.search_entries(since=start, limit=200, timeout=15.0)
        out = []
        for e in entries:
            if e.created_at and e.created_at.replace(tzinfo=e.created_at.tzinfo or timezone.utc) >= end:
                continue
            out.append({"id": e.id, "title": e.title, "author": e.author, "logbooks": e.logbooks,
                        "time": _iso(e.created_at) if e.created_at else None})
        return sorted(out, key=lambda x: x["time"] or "")


# --- rendering --------------------------------------------------------------------


def _fmt(v: Any) -> str:
    n = _num(v)
    if n is None:
        return str(v)
    if n != 0 and (abs(n) < 1e-3 or abs(n) >= 1e5):
        return f"{n:.2e}"
    return f"{n:.3g}"


def _changed(row: dict, role: str) -> int:
    return len((row.get(role) or {}).get("changes") or [])


def _moves(row: dict) -> int:
    """A move is the motor leaving done (DMOV going to 0)."""
    return sum(1 for _, v in (row.get("done_moving") or {}).get("changes") or [] if _num(v) == 0)


def render(facts: dict[str, Any]) -> tuple[str, str]:
    """Title and Markdown body of the logbook entry, from the facts alone."""
    day = facts["start"][:10]
    title = f"{facts['beamline']} daily report {day}"
    lines = [f"Automatic report for {facts['start']} to {facts['end']} (UTC), computed from the archiver. "
             f"{facts['pvs_read']} PVs read; {facts['pvs_without_history']} had no history in the window.", ""]
    if facts.get("beam"):
        b = facts["beam"]
        lines += ["## Beam", ""]
        if b.get("above") is not None and b["max"] is not None and b["max"] < b["above"]:
            lines.append(f"No beam: `{b['pv']}` never reached {_fmt(b['above'])} (max {_fmt(b['max'])}).")
        else:
            lines.append(f"`{b['pv']}`: mean {_fmt(b['mean'])}, max {_fmt(b['max'])}.")
        lines.append("")
    trips = {k: v for k, v in facts.get("trips", {}).items() if v}
    lines += ["## Trips", ""]
    if trips:
        lines += ["| Trip | Count | Last |", "| --- | --- | --- |"]
        lines += [f"| {k} | {len(v)} | {v[-1]['time']} `{v[-1]['pv']}` |" for k, v in trips.items()]
    else:
        lines += ["None recorded."]
    lines.append("")

    groups = facts.get("groups", {})
    if mags := groups.get("magnets"):
        changed = sorted((r for r in mags["rows"] if _changed(r, "setpoint")), key=lambda r: -_changed(r, "setpoint"))
        lines += ["## Magnets", "", f"{mags['devices']} power supplies; {len(changed)} had setpoint changes.", ""]
        if changed:
            lines += ["| Magnet | Function | Setpoint changes | Start | End |", "| --- | --- | --- | --- | --- |"]
            lines += [f"| {r['device']} | {r.get('function') or ''} | {_changed(r, 'setpoint')} | "
                      f"{_fmt(r['setpoint']['first'])} | {_fmt(r['setpoint']['last'])} |" for r in changed[:MAX_ROWS]]
            lines.append("")
    if mots := groups.get("motors"):
        moved = sorted((r for r in mots["rows"] if _moves(r)), key=lambda r: -_moves(r))
        lines += ["## Motors", "", f"{mots['devices']} motors; {len(moved)} moved.", ""]
        if moved:
            lines += ["| Motor | Function | Moves | Start | End |", "| --- | --- | --- | --- | --- |"]
            lines += [f"| {r['device']} | {r.get('function') or ''} | {_moves(r)} | "
                      f"{_fmt((r.get('readback') or {}).get('first'))} | {_fmt((r.get('readback') or {}).get('last'))} |"
                      for r in moved[:MAX_ROWS]]
            lines.append("")
    if cams := groups.get("cameras"):
        used = [r for r in cams["rows"] if (r.get("acquiring") or {}).get("share", {}).get("1", 0) > 0
                or _num((r.get("acquiring") or {}).get("last")) == 1]
        lines += ["## Cameras", "", f"{cams['devices']} cameras; {len(used)} acquired during the window.", ""]
        if used:
            lines += ["| Camera | Acquiring (h) | Median rate (Hz) |", "| --- | --- | --- |"]
            hours = (datetime.fromisoformat(facts["end"].replace("Z", "+00:00"))
                     - datetime.fromisoformat(facts["start"].replace("Z", "+00:00"))).total_seconds() / 3600
            lines += [f"| {r['device']} | {_fmt((r['acquiring'].get('share') or {}).get('1', 0) * hours)} | "
                      f"{_fmt((r.get('frame_rate') or {}).get('median'))} |" for r in used[:MAX_ROWS]]
            lines.append("")
    if (point := groups.get("pointing")) and point.get("cameras"):
        lines += ["## Laser pointing", "", "| Camera | Centroid X range (px) | Centroid Y range (px) |",
                  "| --- | --- | --- |"]
        for r in point["cameras"][:MAX_ROWS]:
            cx, cy = r.get("centroid_x") or {}, r.get("centroid_y") or {}
            rx = _fmt(cx["max"] - cx["min"]) if cx.get("max") is not None else "-"
            ry = _fmt(cy["max"] - cy["min"]) if cy.get("max") is not None else "-"
            lines.append(f"| {r['camera']} | {rx} | {ry} |")
        lines.append("")
    if vac := groups.get("vacuum"):
        worst = sorted((r for r in vac["rows"] if (r.get("pressure") or {}).get("max") is not None),
                       key=lambda r: -r["pressure"]["max"])
        lines += ["## Vacuum", "", f"{vac['devices']} gauges and pumps.", ""]
        if worst:
            lines += ["| Device | Max pressure | Median | Units |", "| --- | --- | --- | --- |"]
            lines += [f"| {r['device']} | {_fmt(r['pressure']['max'])} | {_fmt(r['pressure']['median'])} | "
                      f"{r['pressure'].get('units') or ''} |" for r in worst[:10]]
            lines.append("")
    flags = [(g, f) for g, data in groups.items() for f in data.get("flags", [])]
    lines += ["## Flags", ""]
    if flags:
        lines += ["| Group | Device | What |", "| --- | --- | --- |"]
        lines += [f"| {g} | {f['device']} | {f['kind']} |" for g, f in flags[:MAX_ROWS * 2]]
        if len(flags) > MAX_ROWS * 2:
            lines.append(f"| ... | {len(flags) - MAX_ROWS * 2} more | |")
    else:
        lines.append("None.")
    lines.append("")
    entries = facts.get("logbook") or []
    lines += ["## Logbook", ""]
    if facts.get("logbook_error"):
        lines.append(f"The logbook could not be read: {facts['logbook_error']}")
    else:
        lines.append(f"{len(entries)} entries in the window.")
    for e in entries[:MAX_ROWS]:
        lines.append(f"- {e['time']} #{e['id']} {e['title']} ({e.get('author') or '?'})")
    lines.append("")
    if facts["gaps"]:
        lines += ["## Data gaps", "", f"{len(facts['gaps'])} PVs had no history in the window (not archived "
                  "yet, or their IOC was down), e.g. " + ", ".join(f"`{p}`" for p in facts["gaps"][:10]) + "."]
    return title, "\n".join(lines).strip() + "\n"


def day_window(day: str | None = None, *, tz: timezone = timezone.utc) -> tuple[datetime, datetime]:
    """The 24 hours of `day` (YYYY-MM-DD), or of yesterday."""
    if day:
        start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    else:
        today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=1)
    return start, start + timedelta(days=1)

