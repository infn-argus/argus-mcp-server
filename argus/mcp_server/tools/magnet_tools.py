"""Magnet sets: a settings file into save-and-restore, onto the machine, and into the archiver.

check_magnet_file          read-only: what a file says and what the machine is at now
create_magnet_set          WRITES save-and-restore: configuration (if new) + snapshot of the file's values
apply_magnet_set           WRITES THE MACHINE: a snapshot's set, one pretune step towards it,
                           all to 0 A and STANDBY, or all ON
archive_magnet_pvs         WRITES the archiver: archive the set's PVs

The machine writes confirm in two steps for every client (always_confirm), like a
snapshot restore, and their preview lists each supply from where it is to where it goes.
"""

from __future__ import annotations

from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition
from argus.services.magnet_apply import DEFAULT_TIMEOUT_S, DEFAULT_TOLERANCE, DEFAULT_ZERO_TOLERANCE
from argus.services.magnet_sets import CURRENT_SP, OFF_STATES, STATE_RB, MagnetTarget, target_from_base
from argus.services.magnet_service import MagnetService, is_archived, set_pvs

_PREVIEW_ROWS = 80

_FILE_PROPERTIES = {
    "file_text": {
        "type": "string",
        "description": "The magnet settings file's whole text, exactly as the user gave it (an uploaded or "
        "pasted file): CSV 'Name,Prefix,Current,State' (e.g. 'QUATM001,BTF:MAG:OCEME642,0.33,ON'), or the "
        "legacy .dat 'Name Current Pol StateCode'. Do not summarise or reformat it.",
    },
    "repo": {"type": "string", "description": "Instead of file_text: the git repository holding the file."},
    "path": {"type": "string", "description": "Instead of file_text: the file's path in repo, e.g. "
             "'test-mag/dataset/acc_mixed_02.csv'."},
    "ref": {"type": "string", "description": "Branch, tag or commit of repo (default HEAD)."},
}

_APPLY_SETTINGS = {
    "tolerance": {"type": "number", "description": f"A: |CURRENT_RB - target| counted as reached (default {DEFAULT_TOLERANCE})."},
    "zero_tolerance": {"type": "number", "description": "A: |CURRENT_RB| counted as zero before an ON supply is "
                       f"switched OFF/STANDBY (default {DEFAULT_ZERO_TOLERANCE})."},
    "timeout_s": {"type": "number", "description": "Seconds allowed to each phase (current to 0, state, current) "
                  f"before a supply is reported as not done (default {DEFAULT_TIMEOUT_S:g})."},
}

_ASK_ABOUT_LOGBOOK = (
    "Tell the user what was done, then ASK whether to record it in the logbook. Only if they say yes, call "
    "create_logbook_entry with logbook_suggestion's title and text (adjusted as they wish); it will ask for "
    "its own confirmation."
)


def _service(ctx: AppContext) -> MagnetService:
    if ctx.magnet_service is None:
        raise ValidationError("Magnet sets are not available on this server.")
    return ctx.magnet_service


def _names(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError("magnets must be a list of magnet names.")
    return value


def _settings(arguments: dict[str, Any]) -> dict[str, float]:
    out = {}
    for key, name, default in (("tolerance", "tolerance", DEFAULT_TOLERANCE),
                               ("zero_tolerance", "zero_tolerance", DEFAULT_ZERO_TOLERANCE),
                               ("timeout_s", "timeout", DEFAULT_TIMEOUT_S)):
        value = arguments.get(key, default)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{key} must be a positive number.")
        out[name] = float(value)
    return out


async def _file(arguments: dict[str, Any], ctx: AppContext):
    return await _service(ctx).read_file(
        arguments.get("file_text"), arguments.get("repo"), arguments.get("path"), arguments.get("ref")
    )


def _fmt(state: Any, current: Any) -> str:
    amps = f"{current:.4g} A" if isinstance(current, (int, float)) and not isinstance(current, bool) else "? A"
    return f"{state if state is not None else '?'} {amps}"


async def _moves(service: MagnetService, targets: list[MagnetTarget]) -> tuple[str, int]:
    """Each supply from where it is to where it goes; how many change."""
    live = await service.live(targets)
    lines, changing = [], 0
    for t in targets:
        now = live[t.base]
        state_now, sp_now = now[STATE_RB], now[CURRENT_SP]
        goes_state = t.state or state_now
        goes_current = 0.0 if t.state in OFF_STATES else (t.current if t.current is not None else sp_now)
        same = str(goes_state) == str(state_now) and isinstance(sp_now, (int, float)) and (
            goes_current is None or abs(float(goes_current) - float(sp_now)) <= 0.001)
        if same:
            continue
        changing += 1
        lines.append(f"{t.name}: {_fmt(state_now, sp_now)} -> {_fmt(goes_state, goes_current)}")
    shown = "; ".join(lines[:_PREVIEW_ROWS]) + (f"; ...and {len(lines) - _PREVIEW_ROWS} more" if len(lines) > _PREVIEW_ROWS else "")
    return shown, changing


# --- check ------------------------------------------------------------------

async def _check_magnet_file(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    parsed, source = await _file(arguments, ctx)
    checked = await _service(ctx).check(parsed.targets)
    return {"source": source, "format": parsed.format, "magnets": len(parsed.targets),
            "problems_in_file": parsed.problems, **checked}


# --- save-and-restore ---------------------------------------------------------

async def _describe_create(arguments: dict[str, Any], ctx: AppContext) -> str:
    parsed, source = await _file(arguments, ctx)
    plan = await _service(ctx).plan_set(str(arguments.get("name") or ""), str(arguments.get("folder") or ""), parsed.targets)
    config = (f"use the existing configuration {plan['path']}" if plan["config"] else
              f"create the configuration {plan['path']} ({len(plan['pv_list'])} PVs: CURRENT_SP and STATE_SP of each "
              "supply, with their readbacks)")
    problems = f" Notes on the file: {' '.join(parsed.problems[:5])}" if parsed.problems else ""
    return (f"Under your name, {config}, and save the snapshot {plan['name']!r} in it with the values of {source} "
            f"for {len(parsed.targets)} magnet power supplies. Nothing on the machine is changed.{problems}")


async def _create_magnet_set(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    parsed, source = await _file(arguments, ctx)
    result = await _service(ctx).create_set(
        str(arguments.get("name") or ""), str(arguments.get("folder") or ""), parsed.targets,
        source=source, comment=str(arguments.get("comment") or ""),
    )
    return {**result, "problems_in_file": parsed.problems, "next_step": _ASK_ABOUT_LOGBOOK}


# --- the machine -------------------------------------------------------------

async def _resolved(arguments: dict[str, Any], ctx: AppContext):
    return await _service(ctx).resolve_action(
        str(arguments.get("action") or ""), str(arguments.get("snapshot") or ""),
        str(arguments.get("configuration") or ""), _names(arguments.get("magnets")),
    )


async def _describe_apply(arguments: dict[str, Any], ctx: AppContext) -> str:
    if arguments.get("action") == "pretune":
        return await _describe_pretune(arguments, ctx)
    service = _service(ctx)
    what, targets = await _resolved(arguments, ctx)
    problems = await service.limit_problems(targets)
    if problems:
        raise ValidationError("Not possible: " + "; ".join(problems) + ".")
    shown, changing = await _moves(service, targets)
    head = f"WRITE TO THE LIVE MACHINE, put {what} ({len(targets)} power supplies)"
    if not changing:
        return head + ": every supply is already there, so nothing would change."
    return (head + f": {changing} change (state current now -> after): {shown}. An ON supply going to "
            "STANDBY/OFF is first brought to 0 A; states are set before currents, and readbacks are waited for.")


async def _apply_magnet_set(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    if arguments.get("action") == "pretune":
        return await _pretune_step(arguments, ctx)
    service = _service(ctx)
    what, targets = await _resolved(arguments, ctx)
    result = await service.apply(targets, zero_off_setpoints=arguments.get("action") == "standby", **_settings(arguments))
    summary = result.summary(len(targets))
    return {
        "action": what,
        **result.as_dict(len(targets)),
        "summary": summary,
        "logbook_suggestion": {"title": f"Magnets: {arguments.get('action')} ({len(targets)} supplies)",
                               "text": f"Put {what}. {summary}"},
        "next_step": _ASK_ABOUT_LOGBOOK,
    }


async def _pretune_targets(arguments: dict[str, Any], ctx: AppContext):
    service = _service(ctx)
    steps_left = arguments.get("steps_left")
    if not isinstance(steps_left, int) or isinstance(steps_left, bool):
        raise ValidationError("action 'pretune' needs steps_left: the steps still to go, the user's N on the first step.")
    label, targets = await service.targets_from_snapshot(str(arguments.get("snapshot") or ""),
                                                          str(arguments.get("configuration") or ""))
    names = _names(arguments.get("magnets"))
    if names:
        wanted = {n.lower() for n in names}
        targets = [t for t in targets if t.name.lower() in wanted]
        if not targets:
            raise ValidationError(f"None of {', '.join(names)} is in {label}.")
    return label, targets, await service.pretune_targets(targets, steps_left), steps_left


async def _describe_pretune(arguments: dict[str, Any], ctx: AppContext) -> str:
    service = _service(ctx)
    label, targets, step, steps_left = await _pretune_targets(arguments, ctx)
    problems = await service.limit_problems(step)
    if problems:
        raise ValidationError("Not possible: " + "; ".join(problems) + ".")
    shown, changing = await _moves(service, step)
    which = "the LAST step, onto the set" if steps_left == 1 else f"1/{steps_left} of the way, {steps_left - 1} step(s) after this one"
    if not changing:
        return f"Pretune towards {label}: every supply is already at this step, nothing would change."
    return (f"WRITE TO THE LIVE MACHINE, pretune towards {label}, {which}: {changing} of {len(targets)} power "
            f"supplies change (state current now -> after): {shown}.")


async def _pretune_step(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    service = _service(ctx)
    label, targets, step, steps_left = await _pretune_targets(arguments, ctx)
    result = await service.apply(step, force_current=True, **_settings(arguments))
    summary = result.summary(len(step))
    done = steps_left == 1 and not result.incomplete
    return {
        "towards": label,
        "steps_left_before": steps_left,
        "steps_left_after": steps_left - 1,
        **result.as_dict(len(step)),
        "summary": summary,
        "next_step": (
            "The set is reached. " + _ASK_ABOUT_LOGBOOK if done else
            "Report the result. If it is complete and the user asks for the next step, call apply_magnet_set again "
            f"with action 'pretune' and steps_left={steps_left - 1} (it asks for its own confirmation). If anything did not complete, say so and "
            "do not continue unless the user decides to."
        ),
        **({"logbook_suggestion": {"title": f"Pretune to {label} done",
                                   "text": f"Pretuned {len(targets)} magnet power supplies to {label}. {summary}"}}
           if done else {}),
    }


# --- archiver ------------------------------------------------------------------

async def _archive_pvs(arguments: dict[str, Any], ctx: AppContext) -> list[str]:
    service = _service(ctx)
    if arguments.get("configuration"):
        _, bases = await service.magnets_of(str(arguments["configuration"]))
        targets = [target_from_base(b, None, None) for b in bases]
    elif arguments.get("file_text") or arguments.get("path"):
        targets = (await _file(arguments, ctx))[0].targets
    else:
        raise ValidationError("Give the magnet file (file_text, or repo and path) or a save-and-restore configuration.")
    return set_pvs(targets, include_readbacks=arguments.get("include_readbacks", True) is not False)


async def _describe_archive(arguments: dict[str, Any], ctx: AppContext) -> str:
    pvs = await _archive_pvs(arguments, ctx)
    status = await _service(ctx).archive_status(pvs)
    todo = [p for p in pvs if not is_archived(status.get(p))]
    if not todo:
        return f"All {len(pvs)} PVs are already archived: nothing to do."
    shown = ", ".join(todo[:30]) + (f" ...and {len(todo) - 30} more" if len(todo) > 30 else "")
    return (f"Ask the Archiver Appliance to start archiving {len(todo)} PV(s), with its default sampling: {shown}. "
            f"{len(pvs) - len(todo)} are already archived.")


async def _archive_magnet_pvs(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pvs = await _archive_pvs(arguments, ctx)
    return {**await _service(ctx).archive(pvs), "next_step": _ASK_ABOUT_LOGBOOK}


TOOLS = [
    ToolDefinition(
        name="check_magnet_file",
        description=(
            "Read a magnet settings file (CSV 'Name,Prefix,Current,State', or the legacy .dat) and compare it with "
            "the machine now: each power supply's target state and current, its live state, setpoint and readback, "
            "which do not answer, and targets outside their current limits. Read-only. Use it first when the user "
            "gives such a file, before saving it or applying it."
        ),
        input_schema={"type": "object", "properties": dict(_FILE_PROPERTIES)},
        handler=_check_magnet_file,
    ),
    ToolDefinition(
        name="create_magnet_set",
        description=(
            "WRITES save-and-restore: turns a magnet settings file into a save-and-restore configuration (the "
            "CURRENT_SP and STATE_SP of each supply, with readbacks), if one of that name does not exist yet in the "
            "folder, and a snapshot of the SAME name holding the FILE's values (not the live ones), under the user's "
            "own name. The machine is not touched. ASK the user for the name (and the folder) when they did not give "
            "them; never make one up. An existing configuration of that name is reused only if it has exactly the "
            "file's PVs. Apply the set afterwards with apply_magnet_set or pretune_magnet_step."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_FILE_PROPERTIES,
                "name": {"type": "string", "description": "Name of the configuration and of its snapshot, as the user chose it."},
                "folder": {"type": "string", "description": "Existing save-and-restore folder: a path such as "
                           "'BTF_CONF' or 'BTF_CONF/Magnets', or its uniqueId."},
                "comment": {"type": "string", "description": "Optional comment for the snapshot."},
            },
            "required": ["name", "folder"],
        },
        handler=_create_magnet_set,
        writes=True,
        describe=_describe_create,
    ),
    ToolDefinition(
        name="apply_magnet_set",
        description=(
            "WRITES TO THE LIVE MACHINE, the magnet power supplies, in the consoles' order: an ON supply going to "
            "STANDBY/OFF is first brought to 0 A, states are set before currents, and readbacks are waited for "
            "(within tolerance, up to timeout_s per phase). action 'set': put every supply at a save-and-restore "
            "snapshot's state and current ('put all to <set>'). action 'pretune': ONE step towards that snapshot, the "
            "user deciding each next one: each supply moves 1/steps_left of the way from its setpoint now, so for "
            "'pretune in N steps' the first call has steps_left=N, the next N-1, and steps_left=1 lands on the set; "
            "a supply that must end STANDBY/OFF is brought down while ON and switched only on the last step, one that "
            "must end ON is switched ON on the first. action 'standby': every supply of a configuration to 0 A and "
            "STANDBY. action 'on': every supply of a configuration ON, currents unchanged. Optionally only some "
            "magnets. Use ONLY when the user explicitly asks; always two steps for every client: the first call "
            "changes nothing and lists each supply from now to after - show it, and apply only after the user "
            "confirms. Report supplies that did not complete; never take the next pretune step unasked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["set", "pretune", "standby", "on"]},
                "snapshot": {"type": "string", "description": "actions 'set' and 'pretune': the snapshot's uniqueId, "
                             "or its name or 'latest' together with configuration."},
                "steps_left": {"type": "integer", "minimum": 1, "description": "action 'pretune': steps still to go, "
                               "this one included (N on the first call, 1 on the last)."},
                "configuration": {"type": "string", "description": "The save-and-restore configuration (path such as "
                                  "'BTF_CONF/MySet', or uniqueId): whose magnets for 'standby'/'on', or the snapshot's "
                                  "for 'set' by name."},
                "magnets": {"type": "array", "items": {"type": "string"}, "description": "Only these magnets (names)."},
                **_APPLY_SETTINGS,
            },
            "required": ["action"],
        },
        handler=_apply_magnet_set,
        writes=True,
        always_confirm=True,
        describe=_describe_apply,
    ),
    ToolDefinition(
        name="archive_magnet_pvs",
        description=(
            "WRITES the Archiver Appliance: asks it to archive the PVs of a magnet set (CURRENT_SP, CURRENT_RB, "
            "STATE_SP, STATE_RB of each supply; setpoints only with include_readbacks false), from a magnet file or "
            "a save-and-restore configuration. PVs already archived are left alone. Use only when the user asks to "
            "archive them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                **_FILE_PROPERTIES,
                "configuration": {"type": "string", "description": "Instead of a file: the save-and-restore configuration whose magnets' PVs to archive."},
                "include_readbacks": {"type": "boolean", "description": "Also the readbacks (default true)."},
            },
        },
        handler=_archive_magnet_pvs,
        writes=True,
        describe=_describe_archive,
    ),
]
