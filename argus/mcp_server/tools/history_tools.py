from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

# 120 points draw a clear chart; a model that writes the chart re-types every
# point (~2.5k tokens here), and 300 took long enough to hit LibreChat's
# upstream stream cutoff (~5 min) on qwen38-27b-fp8.
_DEFAULT_POINTS = 120
_MIN_POINTS = 10
_MAX_POINTS = 2000

_GET_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "pv_name": {"type": "string", "description": "The PV to retrieve historical trend data for."},
        "start": {"type": "string", "description": "ISO 8601 start time. Defaults to 1 hour ago."},
        "end": {"type": "string", "description": "ISO 8601 end time. Defaults to now."},
        "max_points": {
            "type": "integer",
            "minimum": _MIN_POINTS,
            "maximum": _MAX_POINTS,
            "description": f"Most points to return (default {_DEFAULT_POINTS}). A range with more samples is "
            "down-sampled by the archiver into time bins (mean, min, max, count), so any range - an hour or a "
            "month - fits. Raise it only for a finer chart.",
        },
    },
    "required": ["pv_name"],
}

_GET_ALARM_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "device_name": {"type": "string", "description": "The device to retrieve recent alarm history for."},
    },
    "required": ["device_name"],
}

_CREATE_LOGBOOK_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short title for the logbook entry."},
        "text": {"type": "string", "description": "The entry's body text."},
        "logbooks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Which logbook(s) to post to - existing logbook names of this facility's Olog, "
            "which differ between facilities (don't assume 'Operations'). At least one required. If a name "
            "is wrong, the error lists the valid ones.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tags, e.g. ['alarm'] or ['maintenance'] — used by get_alarm_history/get_maintenance_history filtering.",
        },
    },
    "required": ["title", "text", "logbooks"],
}


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid ISO 8601 timestamp: {value!r}") from exc


async def _get_history(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pv_name = arguments.get("pv_name")
    if not pv_name or not isinstance(pv_name, str):
        raise ValidationError("pv_name cannot be empty and must be a string.")

    now = datetime.now(timezone.utc)
    start = _parse_time(arguments.get("start"), now - timedelta(hours=1))
    end = _parse_time(arguments.get("end"), now)

    max_points = arguments.get("max_points", _DEFAULT_POINTS)
    if isinstance(max_points, bool) or not isinstance(max_points, int) or not _MIN_POINTS <= max_points <= _MAX_POINTS:
        raise ValidationError(f"max_points must be an integer between {_MIN_POINTS} and {_MAX_POINTS}.")
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start >= end:
        raise ValidationError("start must be before end.")

    series = await ctx.history_service.get_history(pv_name, start, end, max_points=max_points)
    payload = _history_payload(series, start, end)
    viewer = ctx.history_service.archiver_viewer_url(pv_name, start, end)
    if viewer:
        payload["viewer_url"] = viewer
    return payload


def _num(value: Any) -> Any:
    """Round to 6 significant digits: archiver floats carry noise digits that only cost tokens."""
    return float(f"{value:.6g}") if isinstance(value, float) else value


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _history_payload(series: Any, start: datetime, end: datetime) -> dict[str, Any]:
    """Compact, chart-ready: one row per point, columns named once."""
    samples = series.samples
    payload: dict[str, Any] = {
        "pv_name": series.pv_name,
        "units": series.units,
        "start": _iso(start),
        "end": _iso(end),
    }
    if not samples:
        payload.update(mode="empty", point_count=0, points=[],
                       note="No archived samples in this range: the PV may not be archived, or did not change.")
        return payload

    if series.binned:
        # A range answered raw has no bins; a binned reply may still hold a few raw points.
        rows = [
            [_iso(s.timestamp), _num(s.value),
             _num(s.min if s.min is not None else s.value),
             _num(s.max if s.max is not None else s.value),
             s.count if s.count is not None else 1]
            for s in samples
        ]
        payload.update(
            mode="binned",
            samples_in_range=sum(r[4] for r in rows),
            point_count=len(rows),
            columns=["time", "mean", "min", "max", "count"],
            points=rows,
            note="Each point is a time bin of the archived samples. For a chart, draw mean as the line and "
            "min..max as a band, so spikes the mean hides stay visible. Times are UTC.",
        )
        lows = [r[2] for r in rows if isinstance(r[2], (int, float))]
        highs = [r[3] for r in rows if isinstance(r[3], (int, float))]
    else:
        rows = [[_iso(s.timestamp), _num(s.value)] for s in samples]
        payload.update(
            mode="raw",
            samples_in_range=len(rows),
            point_count=len(rows),
            columns=["time", "value"],
            points=rows,
            note="Every archived sample in the range (the archiver stores a sample only when the value "
            "changes, so hold each value until the next point). Times are UTC.",
        )
        lows = highs = [r[1] for r in rows if isinstance(r[1], (int, float))]
        alarms = [[_iso(s.timestamp), s.severity] for s in samples if s.severity]
        if alarms:
            payload["alarm_points"] = alarms[:50]

    if lows and highs:
        payload["summary"] = {
            "min": min(lows),
            "max": max(highs),
            "first": rows[0][1],
            "last": rows[-1][1],
        }
    return payload


async def _get_alarm_history(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    device_name = arguments.get("device_name")
    if not device_name or not isinstance(device_name, str):
        raise ValidationError("device_name cannot be empty and must be a string.")

    entries = await ctx.history_service.get_alarm_history(device_name)
    return {"entries": [dataclasses.asdict(e) for e in entries]}


_ENTRY_ID_DESC = (
    "The entry's Olog id (e.g. 1234), or the Knowledge Hub's code for it (e.g. OLOG-SPARC-1234, as "
    "search_documentation and get_alarm_history show it)."
)

_GET_LOGBOOK_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {"entry_id": {"type": "string", "description": _ENTRY_ID_DESC}},
    "required": ["entry_id"],
}

_UPDATE_LOGBOOK_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "entry_id": {"type": "string", "description": _ENTRY_ID_DESC},
        "title": {"type": "string", "description": "New title. Omit to keep the current one."},
        "text": {
            "type": "string",
            "description": "New full text, replacing the current text. Omit to keep it. To add to an entry, "
            "read it first with get_logbook_entry and send the old text plus the addition.",
        },
        "logbooks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "New full list of logbooks. Omit to keep the current ones.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "New full list of tags. Omit to keep the current ones.",
        },
    },
    "required": ["entry_id"],
}


async def _get_logbook_entry(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    entry_id = arguments.get("entry_id")
    if entry_id in (None, ""):
        raise ValidationError("entry_id is required.")
    entry = await ctx.history_service.get_logbook_entry(entry_id)
    return dataclasses.asdict(entry)


async def _update_logbook_entry(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    entry_id = arguments.get("entry_id")
    if entry_id in (None, ""):
        raise ValidationError("entry_id is required.")
    changes = {k: arguments[k] for k in ("title", "text", "logbooks", "tags") if arguments.get(k) is not None}
    if not changes:
        raise ValidationError("Nothing to change: give at least one of title, text, logbooks, tags.")
    for key in ("logbooks", "tags"):
        if key in changes and not isinstance(changes[key], list):
            raise ValidationError(f"{key} must be a list of strings.")
    entry = await ctx.history_service.update_logbook_entry(entry_id, **changes)
    return dataclasses.asdict(entry)


def _describe_create(arguments: dict[str, Any]) -> str:
    logbooks = ", ".join(arguments.get("logbooks") or []) or "?"
    tags = f", tags {', '.join(arguments['tags'])}" if arguments.get("tags") else ""
    return (
        f"Post a NEW entry to the {logbooks} logbook{tags}, under your name: "
        f"title {arguments.get('title')!r}, text {arguments.get('text')!r}."
    )


def _describe_update(arguments: dict[str, Any]) -> str:
    parts = [f"{k} → {arguments[k]!r}" for k in ("title", "text", "logbooks", "tags") if arguments.get(k) is not None]
    return f"Change logbook entry {arguments.get('entry_id')} under your name (the old version is kept as history): " + "; ".join(parts) + "."


async def _create_logbook_entry(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    title = arguments.get("title")
    text = arguments.get("text")
    logbooks = arguments.get("logbooks")
    if not title or not isinstance(title, str):
        raise ValidationError("title cannot be empty and must be a string.")
    if not text or not isinstance(text, str):
        raise ValidationError("text cannot be empty and must be a string.")
    if not logbooks or not isinstance(logbooks, list):
        raise ValidationError("logbooks cannot be empty and must be a list of strings.")

    entry = await ctx.history_service.create_logbook_entry(title, text, logbooks, tags=arguments.get("tags"))
    return dataclasses.asdict(entry)


TOOLS = [
    ToolDefinition(
        name="get_history",
        description=(
            "Historical trend of a PV over a time range, from the Archiver Appliance - for questions like 'what "
            "did X do last night' and for charts. Returns compact rows (time, value) with a min/max/first/last "
            "summary; a long range is down-sampled by the archiver into at most max_points time bins "
            "(mean, min, max, count), so a month costs the same as an hour. viewer_url opens the same PV and "
            "range in the archiver's own interactive viewer (full resolution, zoom, instant): give it to the "
            "user whenever they want to see or plot a trend. Build a chart in the chat only if they ask for "
            "one there, from these points as they are - don't thin them further by hand."
        ),
        input_schema=_GET_HISTORY_SCHEMA,
        handler=_get_history,
    ),
    ToolDefinition(
        name="get_alarm_history",
        description="Get recent alarm-tagged logbook entries for a device.",
        input_schema=_GET_ALARM_HISTORY_SCHEMA,
        handler=_get_alarm_history,
    ),
    ToolDefinition(
        name="create_logbook_entry",
        description=(
            "WRITES: posts a NEW, permanent entry to the operations logbook, under the user's own name when "
            "they are signed in (e.g. to record an observation, an action taken, or a handover note). Use it "
            "only when the user explicitly asks to log/record/note something - never to read, list or search "
            "the logbook, never as a test, never as a side effect of an unrelated question. To change an "
            "existing entry use update_logbook_entry instead. Tag with 'alarm' or 'maintenance' if relevant "
            "so it surfaces in get_alarm_history/get_maintenance_history later."
        ),
        input_schema=_CREATE_LOGBOOK_ENTRY_SCHEMA,
        handler=_create_logbook_entry,
        writes=True,
        describe=_describe_create,
    ),
    ToolDefinition(
        name="get_logbook_entry",
        description=(
            "Read ONE logbook entry by id straight from Olog (title, text, logbooks, tags, author, date) - "
            "typically before changing it with update_logbook_entry. Read-only. To find entries, use "
            "get_alarm_history or search_documentation (on a Knowledge Hub server, logbook entries are "
            "documents with codes like OLOG-SPARC-1234)."
        ),
        input_schema=_GET_LOGBOOK_ENTRY_SCHEMA,
        handler=_get_logbook_entry,
    ),
    ToolDefinition(
        name="update_logbook_entry",
        description=(
            "WRITES: changes an EXISTING logbook entry (title, text, logbooks or tags), under the user's own "
            "name; Olog keeps the previous version as history. Use it only when the user explicitly asks to "
            "correct or amend an entry. Read the entry first with get_logbook_entry; text replaces the whole "
            "text, so to add something send the old text plus the addition. Never use it to read."
        ),
        input_schema=_UPDATE_LOGBOOK_ENTRY_SCHEMA,
        handler=_update_logbook_entry,
        writes=True,
        describe=_describe_update,
    ),
]
