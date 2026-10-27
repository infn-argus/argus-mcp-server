"""Phoebus save-and-restore tools: search/browse configurations and
snapshots and read their PV values; create configurations (from devices'
setpoint PVs) and snapshots (of an existing configuration), as the
signed-in user. Never apply (restore) a snapshot — see
providers/saverestore/saverestore.py for why.

The two creating tools are writes: two-step confirmation for clients that
ask for it, with a preview listing what will really be saved. Their results
carry a logbook_suggestion, and the model is told to ask the user whether
to record the operation in the logbook.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

_SEARCH_SNAPSHOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Free-text search across all folders/configurations/snapshots, e.g. a configuration name "
                "like 'MAGNET_SP'. Omit and pass node_id instead to list a specific folder's children."
            ),
        },
        "node_id": {
            "type": "string",
            "description": "A folder node's uniqueId (from a previous search_snapshots call) to list its direct children instead of searching.",
        },
        "configuration": {
            "type": "string",
            "description": "A configuration's path (e.g. 'BTF_CONF/Quadrupoles') or uniqueId: list its snapshots, "
            "newest first, with when, who and the comment - 'which snapshots are there of X'.",
        },
    },
}

_GET_SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "node_id": {
            "type": "string",
            "description": "A CONFIGURATION or SNAPSHOT node's uniqueId, from search_snapshots.",
        },
    },
    "required": ["node_id"],
}


async def _search_snapshots(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    configuration = arguments.get("configuration")
    if configuration:
        config, path, snapshots = await ctx.saverestore_service.list_snapshots(str(configuration))
        return {
            "configuration": {"unique_id": config.unique_id, "path": path},
            "snapshot_count": len(snapshots),
            "snapshots": [
                {
                    "unique_id": n.unique_id,
                    "name": n.name,
                    "created": n.created,
                    "owner": n.user_name,
                    "comment": n.description,
                    "node_type": n.node_type,
                }
                for n in snapshots
            ],
        }

    node_id = arguments.get("node_id")
    if node_id:
        nodes = await ctx.saverestore_service.get_children(node_id)
        return {"nodes": [dataclasses.asdict(n) for n in nodes]}

    query = arguments.get("query")
    if not query or not isinstance(query, str):
        raise ValidationError(
            "Provide query (to search), node_id (to list a folder's children) or configuration (to list its snapshots)."
        )
    nodes = await ctx.saverestore_service.search(query)
    return {"nodes": [dataclasses.asdict(n) for n in nodes]}


async def _get_snapshot(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    node_id = arguments.get("node_id")
    if not node_id or not isinstance(node_id, str):
        raise ValidationError("node_id cannot be empty and must be a string.")

    node = await ctx.saverestore_service.get_node(node_id)
    if node.node_type == "SNAPSHOT":
        snapshot = await ctx.saverestore_service.get_snapshot(node_id)
        return {"node": dataclasses.asdict(node), "snapshot": dataclasses.asdict(snapshot)}
    if node.node_type == "CONFIGURATION":
        config = await ctx.saverestore_service.get_configuration(node_id)
        return {"node": dataclasses.asdict(node), "configuration": dataclasses.asdict(config)}
    return {
        "node": dataclasses.asdict(node),
        "message": "This is a FOLDER node — use search_snapshots with node_id set to this id to list its children.",
    }


_CREATE_CONFIGURATION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Name of the new configuration (unique in its folder)."},
        "folder": {
            "type": "string",
            "description": "Existing save-and-restore folder to create it in: a path from the root such as "
            "'BTF_CONF' or 'BTF_CONF/Magnets', or a folder's uniqueId. Folders are not created; a wrong one "
            "is answered with the folders that exist there.",
        },
        "devices": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Device names from list_beamline_devices. Each contributes its setpoint PVs (e.g. a "
            "magnet's CURRENT_SP and STATE_SP), paired with their readbacks if include_readbacks.",
        },
        "include_readbacks": {
            "type": "boolean",
            "description": "Also record each setpoint's readback PV (e.g. CURRENT_RB), saved alongside it in "
            "snapshots for comparison; never restored. Default true.",
        },
        "pvs": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    {
                        "type": "object",
                        "properties": {"setpoint": {"type": "string"}, "readback": {"type": "string"}},
                        "required": ["setpoint"],
                    },
                ]
            },
            "description": "Extra PVs given explicitly - for devices without a setpoint convention (motors, "
            "vacuum): a PV name, or {setpoint, readback}.",
        },
        "description": {"type": "string", "description": "Optional description of the configuration."},
    },
    "required": ["name", "folder"],
}

_TAKE_SNAPSHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "configuration": {
            "type": "string",
            "description": "The existing configuration to snapshot: its path such as 'BTF_CONF/Quadrupoles', or "
            "its uniqueId (search_snapshots finds both). A snapshot always belongs to a configuration.",
        },
        "name": {"type": "string", "description": "Snapshot name. Default: the configuration name and the time."},
        "comment": {"type": "string", "description": "Why the snapshot was taken (e.g. 'before the energy change')."},
    },
    "required": ["configuration"],
}

_ASK_ABOUT_LOGBOOK = (
    "Tell the user what was done, then ASK whether to record it in the logbook. Only if they say yes, call "
    "create_logbook_entry with logbook_suggestion's title and text (adjusted as they wish) in one of this "
    "facility's logbooks; it will ask for its own confirmation."
)


async def _plan_text(arguments: dict[str, Any], ctx: AppContext) -> tuple[str, Any]:
    plan = await ctx.saverestore_service.plan_configuration(
        _string_list(arguments.get("devices"), "devices"),
        include_readbacks=arguments.get("include_readbacks", True) is not False,
        pvs=arguments.get("pvs"),
    )
    lines = [f"{pv.pv_name}" + (f" (readback {pv.readback_pv_name})" if pv.readback_pv_name else "") for pv in plan.pv_list]
    return "; ".join(lines), plan


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError(f"{field} must be a list of strings.")
    return value


async def _describe_create_configuration(arguments: dict[str, Any], ctx: AppContext) -> str:
    pv_text, plan = await _plan_text(arguments, ctx)
    if not plan.pv_list:
        skipped = "; ".join(f"{d}: {why}" for d, why in plan.skipped.items()) or "no devices or PVs given"
        raise ValidationError(f"Nothing to save: {skipped}.")
    _, folder_path = await ctx.saverestore_service.resolve_folder(str(arguments.get("folder") or ""))
    skipped = (
        " Not included: " + "; ".join(f"{d} ({why})" for d, why in plan.skipped.items()) + "." if plan.skipped else ""
    )
    return (
        f"Create the save-and-restore configuration {folder_path}/{arguments.get('name')} under your name, "
        f"with {len(plan.pv_list)} PV(s): {pv_text}.{skipped}"
    )


async def _describe_take_snapshot(arguments: dict[str, Any], ctx: AppContext) -> str:
    config, path = await ctx.saverestore_service.resolve_configuration(str(arguments.get("configuration") or ""))
    name = arguments.get("name") or f"{config.name} <now>"
    comment = f", comment {arguments['comment']!r}" if arguments.get("comment") else ""
    return (
        f"Take a snapshot {name!r} of the configuration {path} under your name: the save-and-restore service "
        f"reads its PVs now and saves their values{comment}. Nothing on the machine is changed."
    )


_RESTORE_SCHEMA = {
    "type": "object",
    "properties": {
        "snapshot": {
            "type": "string",
            "description": "The snapshot to restore: its uniqueId, or - together with configuration - its name, "
            "or 'latest' for the configuration's newest snapshot.",
        },
        "configuration": {
            "type": "string",
            "description": "The snapshot's configuration (path such as 'BTF_CONF/Quadrupoles', or uniqueId), when "
            "snapshot is a name or 'latest'.",
        },
    },
    "required": ["snapshot"],
}


async def _describe_restore(arguments: dict[str, Any], ctx: AppContext) -> str:
    preview = await ctx.saverestore_service.restore_preview(
        str(arguments.get("snapshot") or ""), str(arguments.get("configuration") or "")
    )
    node, changes = preview["snapshot"], preview["changes"]
    when = f" (taken {node.created:%Y-%m-%d %H:%M} by {node.user_name or 'unknown'})" if node.created else ""
    head = f"RESTORE the snapshot {preview['label']}{when} to the LIVE machine, under your name: "
    if not changes:
        return head + f"all {preview['unchanged']} PV(s) already match the snapshot, so no value would change."
    shown = "; ".join(
        f"{c['pv_name']}: {c['live']} -> {c['saved']}" + (f" (delta {c['delta']})" if c.get("delta") else "")
        for c in changes[:40]
    )
    more = f" ...and {len(changes) - 40} more" if len(changes) > 40 else ""
    return (
        head + f"{len(changes)} setpoint PV(s) WOULD CHANGE (live -> saved): {shown}{more}. "
        f"{preview['unchanged']} already match. Readbacks and read-only PVs are not written."
    )


async def _restore_saverestore_snapshot(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    snapshot = arguments.get("snapshot")
    if not snapshot or not isinstance(snapshot, str):
        raise ValidationError("snapshot is required (an id, or a name / 'latest' with configuration).")
    result = await ctx.saverestore_service.restore(snapshot, str(arguments.get("configuration") or ""))
    return {**result, "next_step": _ASK_ABOUT_LOGBOOK}


async def _create_saverestore_configuration(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    name = arguments.get("name")
    folder = arguments.get("folder")
    if not name or not isinstance(name, str):
        raise ValidationError("name is required.")
    if not folder or not isinstance(folder, str):
        raise ValidationError("folder is required (an existing folder path or id).")
    result = await ctx.saverestore_service.create_configuration(
        name,
        folder,
        _string_list(arguments.get("devices"), "devices"),
        include_readbacks=arguments.get("include_readbacks", True) is not False,
        pvs=arguments.get("pvs"),
        description=arguments.get("description") or "",
    )
    return {**result, "next_step": _ASK_ABOUT_LOGBOOK}


async def _take_saverestore_snapshot(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    configuration = arguments.get("configuration")
    if not configuration or not isinstance(configuration, str):
        raise ValidationError("configuration is required (a configuration path or id).")
    result = await ctx.saverestore_service.take_snapshot(
        configuration, name=arguments.get("name") or "", comment=arguments.get("comment") or ""
    )
    return {**result, "next_step": _ASK_ABOUT_LOGBOOK}


TOOLS = [
    ToolDefinition(
        name="search_snapshots",
        description=(
            "Search or browse Phoebus save-and-restore: pass query for free-text search across all "
            "folders/configurations/snapshots, or node_id (a folder's uniqueId from a previous result) to "
            "list that folder's direct children. A CONFIGURATION defines which PVs a snapshot covers; a "
            "SNAPSHOT is a saved set of PV values at a point in time. Pass configuration to list a "
            "configuration's snapshots, newest first. Use the returned uniqueId with get_snapshot to read a "
            "configuration's PV list or a snapshot's saved values. Read-only; restoring is "
            "restore_saverestore_snapshot."
        ),
        input_schema=_SEARCH_SNAPSHOTS_SCHEMA,
        handler=_search_snapshots,
    ),
    ToolDefinition(
        name="get_snapshot",
        description=(
            "Get the detail of a save-and-restore node by its uniqueId (from search_snapshots): a "
            "CONFIGURATION's PV list (which PVs/readback PVs it covers, not their values), or a "
            "SNAPSHOT's saved PV values and readback values as of when it was taken."
        ),
        input_schema=_GET_SNAPSHOT_SCHEMA,
        handler=_get_snapshot,
    ),
    ToolDefinition(
        name="create_saverestore_configuration",
        description=(
            "WRITES: creates a save-and-restore configuration (the list of PVs a snapshot will save), under "
            "the user's own name, in an existing folder. Give devices from list_beamline_devices: each "
            "contributes its setpoint PVs, with their readbacks if include_readbacks (default true). PVs of "
            "devices without a setpoint convention can be given explicitly with pvs. Use only when the user "
            "asks to create a configuration. To save current values, take a snapshot of it afterwards with "
            "take_saverestore_snapshot."
        ),
        input_schema=_CREATE_CONFIGURATION_SCHEMA,
        handler=_create_saverestore_configuration,
        writes=True,
        describe=_describe_create_configuration,
    ),
    ToolDefinition(
        name="take_saverestore_snapshot",
        description=(
            "WRITES: takes a snapshot of an EXISTING save-and-restore configuration, under the user's own "
            "name: the save-and-restore service reads the configuration's PVs now and stores their values. "
            "Nothing on the machine changes. A snapshot always belongs to a configuration - to snapshot "
            "devices with no configuration yet, create one first with create_saverestore_configuration. "
            "Never restores anything."
        ),
        input_schema=_TAKE_SNAPSHOT_SCHEMA,
        handler=_take_saverestore_snapshot,
        writes=True,
        describe=_describe_take_snapshot,
    ),
    ToolDefinition(
        name="restore_saverestore_snapshot",
        description=(
            "WRITES TO THE LIVE MACHINE: restores a save-and-restore snapshot - the service writes each saved "
            "setpoint back (never readbacks or read-only PVs), under the user's own name. Use it ONLY when the "
            "user explicitly asks to restore a specific snapshot (or 'the latest' of a named configuration); "
            "never on your own initiative, never to 'check' anything. Always two steps for every client: the "
            "first call changes nothing and returns, PV by PV, what would change from the live value to the "
            "saved one - show that to the user, and restore only after they explicitly confirm it. Afterwards "
            "report any PVs that failed. To find snapshots, use search_snapshots with configuration."
        ),
        input_schema=_RESTORE_SCHEMA,
        handler=_restore_saverestore_snapshot,
        writes=True,
        always_confirm=True,
        describe=_describe_restore,
    ),
]
