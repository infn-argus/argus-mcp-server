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


async def _get_pv(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
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
        description="Get the current value of a PV, including timestamp and alarm severity.",
        input_schema=_PV_NAME_SCHEMA,
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
