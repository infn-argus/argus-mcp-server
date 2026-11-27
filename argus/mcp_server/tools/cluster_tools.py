"""Read-only cluster diagnostics: what is wrong with a pod, or with the node under it."""

from __future__ import annotations

from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

_WINDOW = {
    "type": "integer",
    "description": "Rate window in minutes (1-60, default 5): the report describes the last N minutes.",
}

_INSPECT_POD_SCHEMA = {
    "type": "object",
    "properties": {
        "pod": {"type": "string", "description": "Exact pod name, e.g. sbncamera-03-0. Give this or ioc."},
        "ioc": {"type": "string", "description": "IOC name, e.g. sbncamera-03: its pod is looked up. Give this or pod."},
        "namespace": {"type": "string", "description": "Namespace (the beamline, e.g. sparc). Defaults to the configured one."},
        "window_minutes": _WINDOW,
    },
    "required": [],
}

_INSPECT_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "node": {
            "type": "string",
            "description": "Kubernetes node name, e.g. plk8sdadom003.lnf.infn.it (inspect_pod reports which node a pod is on).",
        },
        "window_minutes": _WINDOW,
    },
    "required": ["node"],
}


_CHECK_CONNECTIVITY_SCHEMA = {
    "type": "object",
    "properties": {
        "ioc": {
            "type": "string",
            "description": (
                "IOC name, e.g. ac2bpm01 or sbncamera-04: its device addresses (host/server/ip, device ip/id, ports) "
                "are read from the beamline config, and the probe also runs inside this IOC's pod."
            ),
        },
        "hosts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Explicit host names or IPv4 addresses (private networks only). With ioc, they are probed from that IOC's pod instead of its configured devices.",
        },
        "port": {"type": "integer", "description": "TCP port to test on every target (overrides configured ports)."},
        "namespace": {"type": "string", "description": "Namespace (the beamline, e.g. sparc). Defaults to the configured one."},
        "vantage": {
            "type": "string",
            "enum": ["auto", "argus", "ioc", "both"],
            "description": (
                "Where to probe from. 'ioc': inside the IOC's pod (its VLANs, routes, firewall position) - the only "
                "conclusive test for devices on private networks such as power-supply LANs. 'argus': from ARGUS's "
                "pod. 'both'/'auto' (default): both when an ioc is given, and compare."
            ),
        },
        "count": {"type": "integer", "description": "Pings per target, 1-5 (default 3)."},
    },
    "required": [],
}


def _window(arguments: dict[str, Any]) -> int:
    value = arguments.get("window_minutes", 5)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError("window_minutes must be an integer.")
    return max(1, min(60, value))


async def _inspect_pod(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    service = ctx.cluster_service
    if service is None:
        raise ValidationError("Cluster diagnostics are not available in this deployment.")
    pod, ioc, namespace = arguments.get("pod"), arguments.get("ioc"), arguments.get("namespace")
    if bool(pod) == bool(ioc):
        raise ValidationError("Give exactly one of pod or ioc.")
    if ioc:
        pod = await service.resolve_ioc_pod(str(ioc), namespace)
    report = await service.inspect_pod(str(pod), namespace, window_minutes=_window(arguments))
    return report.to_dict()


async def _inspect_node(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    service = ctx.cluster_service
    if service is None:
        raise ValidationError("Cluster diagnostics are not available in this deployment.")
    node = arguments.get("node")
    if not node or not isinstance(node, str):
        raise ValidationError("node is required.")
    report = await service.inspect_node(node, window_minutes=_window(arguments))
    return report.to_dict()


async def _check_connectivity(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    service = ctx.connectivity_service
    if service is None:
        raise ValidationError("Connectivity checks are not available in this deployment.")
    hosts = arguments.get("hosts")
    if hosts is not None and (not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts)):
        raise ValidationError("hosts must be a list of strings.")
    port = arguments.get("port")
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        raise ValidationError("port must be an integer 1-65535.")
    count = arguments.get("count", 3)
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValidationError("count must be an integer.")
    ioc = arguments.get("ioc")
    if ioc is not None and not isinstance(ioc, str):
        raise ValidationError("ioc must be a string.")
    report = await service.check(
        ioc=ioc or None,
        hosts=hosts or None,
        port=port,
        namespace=arguments.get("namespace"),
        vantage=arguments.get("vantage", "auto"),
        count=count,
    )
    return report.to_dict()


TOOLS = [
    ToolDefinition(
        name="inspect_pod",
        description=(
            "Diagnose one Kubernetes pod (by pod name, or by IOC name): phase, readiness, container restarts and "
            "why they ended, which node it runs on and that node's state, its networks (Multus extras such as a "
            "camera VLAN on net1, with IPs), placement rules, recent events, and - from Prometheus - receive/send "
            "rates, drops and errors per interface over the last few minutes. Starts with 'findings': plain "
            "statements of what looks wrong. Read-only. For the node's own bandwidth, drops, UDP buffers and CPU, "
            "follow up with inspect_node on the node this reports."
        ),
        input_schema=_INSPECT_POD_SCHEMA,
        handler=_inspect_pod,
    ),
    ToolDefinition(
        name="inspect_node",
        description=(
            "Diagnose one Kubernetes node as a bottleneck: Ready/pressure conditions, taints, placement labels "
            "(e.g. control=sparc-br-cams), every pod on it (and which use extra networks such as camera VLANs), "
            "per-interface receive/send rates vs link speed, drops and errors (bond, VLAN, bridge), UDP "
            "receive-buffer overflows, kernel backlog drops, CPU (including the busiest core's softirq - packet "
            "processing) and memory, over the last few minutes. Starts with 'findings'. Read-only."
        ),
        input_schema=_INSPECT_NODE_SCHEMA,
        handler=_inspect_node,
    ),
    ToolDefinition(
        name="check_connectivity",
        description=(
            "Are an IOC's devices reachable on the network? Pings each device address the IOC's config declares "
            "(host/server/ip, devices' ip, camera ids), tests its TCP port, and for GigE cameras reads the control "
            "register (does the camera answer, does anything control it). Runs inside the IOC's own pod - the same "
            "network position as the IOC, required for devices on private networks only the IOC can reach - and "
            "from ARGUS, then compares: device off, IOC network path broken, host up but service port closed. "
            "Use it when an IOC restarts repeatedly, is disconnected, or its PVs are invalid. Read-only."
        ),
        input_schema=_CHECK_CONNECTIVITY_SCHEMA,
        handler=_check_connectivity,
    ),
]
