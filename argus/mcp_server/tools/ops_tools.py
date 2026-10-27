from __future__ import annotations

import dataclasses
from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

_SEARCH_PVS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "PV name pattern, tag, or property to search for."},
        "limit": {"type": "integer", "description": "Maximum number of results to return."},
    },
    "required": ["query"],
}

_NAMESPACE_SCHEMA = {
    "type": "object",
    "properties": {
        "namespace": {"type": "string", "description": "Kubernetes namespace. Defaults to the configured default."},
    },
    "required": [],
}

_RESTART_IOC_SCHEMA = {
    "type": "object",
    "properties": {
        "pod_name": {"type": "string", "description": "The Kubernetes pod name of the IOC to restart."},
        "namespace": {"type": "string", "description": "Kubernetes namespace the pod runs in."},
    },
    "required": ["pod_name", "namespace"],
}

_EXECUTE_PROCEDURE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The name of the pre-approved operational procedure to run."},
        "params": {"type": "object", "description": "Parameters for the procedure."},
    },
    "required": ["name"],
}


async def _search_pvs(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    query = arguments.get("query")
    if not query or not isinstance(query, str):
        raise ValidationError("query cannot be empty and must be a string.")
    channels = await ctx.operations_service.search_pvs(query, limit=arguments.get("limit", 50))
    return {"channels": [dataclasses.asdict(c) for c in channels]}


async def _list_iocs(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pods = await ctx.operations_service.list_iocs(namespace=arguments.get("namespace"))
    return {"iocs": [dataclasses.asdict(p) for p in pods]}


async def _restart_ioc(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    pod_name = arguments.get("pod_name")
    namespace = arguments.get("namespace")
    if not pod_name or not isinstance(pod_name, str):
        raise ValidationError("pod_name cannot be empty and must be a string.")
    if not namespace or not isinstance(namespace, str):
        raise ValidationError("namespace cannot be empty and must be a string.")

    accepted = await ctx.operations_service.restart_ioc(pod_name, namespace)
    return {"restarted": accepted, "pod_name": pod_name, "namespace": namespace}


async def _execute_procedure(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    name = arguments.get("name")
    if not name or not isinstance(name, str):
        raise ValidationError("name cannot be empty and must be a string.")
    result = await ctx.operations_service.execute_procedure(name, arguments.get("params", {}))
    return {"result": result}


async def _machine_summary(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    summary = await ctx.operations_service.machine_summary(namespace=arguments.get("namespace"))
    return dataclasses.asdict(summary)


def _describe_restart_ioc(arguments: dict[str, Any]) -> str:
    where = f" in namespace {arguments['namespace']}" if arguments.get("namespace") else ""
    return f"Restart the IOC pod {arguments.get('pod_name')}{where} (its PVs go offline until it is back)."


def _describe_procedure(arguments: dict[str, Any]) -> str:
    params = f" with parameters {arguments['params']}" if arguments.get("params") else ""
    return f"Run the operational procedure {arguments.get('name')!r}{params}."


TOOLS = [
    ToolDefinition(
        name="search_pvs",
        description=(
            "DO NOT use this to discover devices, IOCs, zones, or device types (e.g. 'magnets in BTF1', "
            "'what quadrupoles exist') — use list_beamline_devices for that, every time, first. Only use "
            "search_pvs when a specific PV name/tag/property is already what's being asked about (e.g. "
            "'find the PV for QUATB002's current readback'). ChannelFinder's channel naming/tagging does "
            "not correspond to device-type words like 'magnet' or 'quadrupole', so using this tool for "
            "discovery returns wrong/empty results — it searches PVs already registered in ChannelFinder "
            "by name pattern, tag, or property, nothing else."
        ),
        input_schema=_SEARCH_PVS_SCHEMA,
        handler=_search_pvs,
    ),
    ToolDefinition(
        name="list_iocs",
        description="List running IOCs and their Kubernetes pod status.",
        input_schema=_NAMESPACE_SCHEMA,
        handler=_list_iocs,
    ),
    ToolDefinition(
        name="restart_ioc",
        description="Restart an IOC's pod.",
        input_schema=_RESTART_IOC_SCHEMA,
        handler=_restart_ioc,
        writes=True,
        describe=_describe_restart_ioc,
    ),
    ToolDefinition(
        name="machine_summary",
        description="High-level machine-wide summary: IOC health counts and recent alarm count.",
        input_schema=_NAMESPACE_SCHEMA,
        handler=_machine_summary,
    ),
    ToolDefinition(
        name="execute_procedure",
        description="Run a named, pre-approved operational procedure.",
        input_schema=_EXECUTE_PROCEDURE_SCHEMA,
        handler=_execute_procedure,
        writes=True,
        describe=_describe_procedure,
    ),
]
