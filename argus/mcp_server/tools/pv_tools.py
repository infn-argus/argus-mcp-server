"""PV-level tools: the new get_pv/set_pv plus the legacy get_pv_value/set_pv_value/
get_pv_info names and schemas, preserved verbatim for backward compatibility with
the original server_by_stdio.py / server_by_sse.py and the existing langchain
example clients under test/.
"""

from __future__ import annotations

from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

_PV_NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "pv_name": {
            "type": "string",
            "description": "The name of the PV variable provided by the user.",
        }
    },
    "required": ["pv_name"],
}

_MAX_MANY = 500
_MAX_ARRAY_SHOWN = 16

_GET_PV_SCHEMA = {
    "type": "object",
    "properties": {
        "pv_name": {"type": "string", "description": "One PV to read."},
        "pv_names": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _MAX_MANY,
            "description": f"Several PVs to read in one call (up to {_MAX_MANY}), e.g. the same field on every "
            "camera, or a device's setpoints and readbacks. Use this instead of one call per PV.",
        },
        "pattern": {
            "type": "string",
            "description": "Read every PV whose name matches this pattern (looked up in ChannelFinder): * "
            "matches within one ':' level, ** across levels, ? one character, {a,b} either. E.g. "
            "'EUAPS:CAM:*:ArrayRate_RBV' (each camera's frame rate) or "
            "'EUAPS:CAM:*:Overlay1:*:{SizeX,SizeY}_RBV' (every overlay's size). "
            f"Up to {_MAX_MANY} matches.",
        },
    },
}

# Verbatim copy of the original SET_PV_VALUE inputSchema (server_by_stdio.py),
# kept byte-identical so existing clients don't need to change.
_LEGACY_SET_PV_SCHEMA = {
    "type": "object",
    "properties": {
        "pv_name": {
            "type": "string",
            "description": "The name of the PV variable provided by the user.",
        },
        "pv_value": {
            "type": "string",
            "description": "The new PV value provided by the user.",
        },
    },
    "required": ["pv_name", "pv_value"],
}

_SET_PV_SCHEMA = {
    "type": "object",
    "properties": {
        "pv_name": {
            "type": "string",
            "description": "The name of the PV to set.",
        },
        "value": {
            "description": "The new value to write to the PV.",
        },
    },
    "required": ["pv_name", "value"],
}


def _require_pv_name(arguments: dict[str, Any]) -> str:
    pv_name = arguments.get("pv_name")
    if not pv_name or not isinstance(pv_name, str):
        raise ValidationError("PV name cannot be empty and must be a string.")
    return pv_name


def _compact(value: Any) -> Any:
    """Keep the answer small: long arrays (waveforms, images) are summarised."""
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        value = value.tolist()
    if isinstance(value, (list, tuple)) and len(value) > _MAX_ARRAY_SHOWN:
        return {"array_length": len(value), "first": list(value[:_MAX_ARRAY_SHOWN])}
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


async def _get_many(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    names = arguments.get("pv_names")
    pattern = arguments.get("pattern")
    result: dict[str, Any] = {}
    if pattern is not None:
        if names is not None or arguments.get("pv_name"):
            raise ValidationError("Give one of pv_name, pv_names or pattern.")
        if not isinstance(pattern, str) or not pattern.strip() or pattern.strip("*?: ") == "":
            raise ValidationError("pattern must name part of the PVs, e.g. 'EUAPS:CAM:*:ArrayRate_RBV'.")
        names, truncated = await ctx.operations_service.pv_names_matching(pattern.strip(), _MAX_MANY)
        result["pattern"] = pattern.strip()
        if truncated:
            result["truncated"] = f"More than {_MAX_MANY} PVs match: only the first {_MAX_MANY} were read. Narrow the pattern."
        if not names:
            result.update(count=0, values={}, note="No PV in ChannelFinder matches this pattern.")
            return result
    else:
        if arguments.get("pv_name"):
            raise ValidationError("Give one of pv_name, pv_names or pattern.")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
            raise ValidationError("pv_names must be a non-empty list of PV names.")
        if len(names) > _MAX_MANY:
            raise ValidationError(f"At most {_MAX_MANY} PVs per call.")
        names = list(dict.fromkeys(n.strip() for n in names))

    values = await ctx.operations_service.get_pvs(names)
    read = {v.name: _compact(v.value) for v in values if v.status != "disconnected"}
    alarms = {v.name: v.severity for v in values
              if v.status != "disconnected" and v.severity not in (None, "0", "NO_ALARM", "None")}
    disconnected = [v.name for v in values if v.status == "disconnected"]
    result.update(count=len(names), values=read)
    if alarms:
        result["alarms"] = alarms
    if disconnected:
        result["not_answering"] = disconnected
        if pattern is not None:
            result["note"] = (
                "not_answering PVs are listed in ChannelFinder but don't answer now: usually names left from "
                "an IOC that was renamed or stopped."
            )
    return result


async def _get_pv(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    if arguments.get("pv_names") is not None or arguments.get("pattern") is not None:
        return await _get_many(arguments, ctx)
    pv_name = _require_pv_name(arguments)
    result = await ctx.operations_service.get_pv(pv_name)
    return {
        "value": result.value,
        "timestamp": result.timestamp,
        "severity": result.severity,
        "status_field": result.status,
    }


async def _get_pv_value_legacy(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pv_name = _require_pv_name(arguments)
    result = await ctx.operations_service.get_pv(pv_name)
    return {"value": result.value}


async def _set_pv(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pv_name = _require_pv_name(arguments)
    if "value" not in arguments:
        raise ValidationError("Missing required argument: value")
    result = await ctx.operations_service.set_pv(pv_name, arguments["value"])
    return {"message": f"Successfully set PV '{pv_name}' to: {result.new_value}"}


async def _set_pv_value_legacy(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pv_name = _require_pv_name(arguments)
    pv_value = arguments.get("pv_value")
    if not pv_value or not isinstance(pv_value, str):
        raise ValidationError("PV value cannot be empty and must be a string.")
    await ctx.operations_service.set_pv(pv_name, pv_value)
    return {"message": f"Successfully set PV '{pv_name}' value to: {pv_value}"}


async def _get_pv_info(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pv_name = _require_pv_name(arguments)
    info = await ctx.operations_service.get_pv_info(pv_name)
    return {"info": vars(info)}


def _describe_set_pv(arguments: dict[str, Any]) -> str:
    value = arguments.get("value", arguments.get("pv_value"))
    return f"Write {value!r} to PV {arguments.get('pv_name')} (a live change to the machine)."


TOOLS = [
    ToolDefinition(
        name="get_pv",
        description=(
            "Read live PV values. One PV (pv_name: value, timestamp, alarm severity), a list (pv_names, up to "
            f"{_MAX_MANY}) or every PV matching a name pattern (pattern, e.g. 'EUAPS:CAM:*:ArrayRate_RBV'), "
            "in one call. Whenever several PVs are needed - the same field on many devices, many fields of "
            "one device - read them together, never one call per PV."
        ),
        input_schema=_GET_PV_SCHEMA,
        handler=_get_pv,
    ),
    ToolDefinition(
        name="set_pv",
        description="Set the value of a PV.",
        input_schema=_SET_PV_SCHEMA,
        handler=_set_pv,
        writes=True,
        describe=_describe_set_pv,
    ),
    ToolDefinition(
        name="get_pv_info",
        description="Get information about a specific PV.",
        input_schema=_PV_NAME_SCHEMA,
        handler=_get_pv_info,
    ),
    # Legacy aliases — exact name/schema/response-shape compatibility with the
    # original 3-tool server.
    ToolDefinition(
        name="get_pv_value",
        description="Get the value of a specific PV.",
        input_schema=_PV_NAME_SCHEMA,
        handler=_get_pv_value_legacy,
    ),
    ToolDefinition(
        name="set_pv_value",
        description="Set the value of a specific PV.",
        input_schema=_LEGACY_SET_PV_SCHEMA,
        handler=_set_pv_value_legacy,
        writes=True,
        describe=_describe_set_pv,
    ),
]
