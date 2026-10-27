from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

_GET_HISTORY_SCHEMA = {
    "type": "object",
    "properties": {
        "pv_name": {"type": "string", "description": "The PV to retrieve historical trend data for."},
        "start": {"type": "string", "description": "ISO 8601 start time. Defaults to 1 hour ago."},
        "end": {"type": "string", "description": "ISO 8601 end time. Defaults to now."},
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
            "description": "Which logbook(s) to post to, e.g. ['Operations']. At least one required.",
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

    series = await ctx.history_service.get_history(pv_name, start, end)
    return {"history": dataclasses.asdict(series)}


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
        description="Get historical trend data for a PV over a time range (from the Archiver Appliance).",
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
