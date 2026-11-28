"""Prometheus provider — node and pod network/CPU health from the cluster's Prometheus.

node-exporter (per-node interfaces, UDP and softnet counters, CPU) and
cAdvisor (per-pod interfaces, including Multus extras such as ``net1``) are
already scraped by kube-prometheus-stack, so ARGUS reads them here instead of
exec-ing into pods or nodes.

Like the Loki provider, it takes structured arguments only and builds every
PromQL string itself: label values are quoted with ``json.dumps`` (PromQL
string literals follow Go escaping), so a tool argument can't break out of a
matcher and run a query of its own.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import ClassVar

import httpx

from argus.config.settings import PrometheusSettings
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.prometheus.exceptions import (
    PrometheusNodeNotFoundError,
    PrometheusQueryError,
    PrometheusTimeoutError,
    PrometheusUnavailableError,
    PrometheusUnconfiguredError,
)
from argus.providers.prometheus.models import (
    InterfaceTraffic,
    NodeCpuStats,
    NodeNetworkStats,
    PodNetworkStats,
)

# Virtual per-container and overlay interfaces: their traffic is already
# counted on the physical/VLAN/bridge interface the report is about.
_EXCLUDED_DEVICES = "lo|veth.*|cali.*|flannel.*|cni.*|tunl.*|vxlan.*|kube-ipvs.*|docker.*|nodelocaldns|bonding_masters"

_MIN_WINDOW, _MAX_WINDOW = 1, 60

# rate() metric -> InterfaceTraffic field, and the factor to apply (bytes -> bits).
_NODE_INTERFACE_RATES = {
    "node_network_receive_bytes_total": ("rx_bits_per_s", 8),
    "node_network_transmit_bytes_total": ("tx_bits_per_s", 8),
    "node_network_receive_packets_total": ("rx_packets_per_s", 1),
    "node_network_receive_drop_total": ("rx_drops_per_s", 1),
    "node_network_receive_errs_total": ("rx_errors_per_s", 1),
    "node_network_transmit_drop_total": ("tx_drops_per_s", 1),
    "node_network_transmit_errs_total": ("tx_errors_per_s", 1),
}
_POD_INTERFACE_RATES = {
    "container_network_receive_bytes_total": ("rx_bits_per_s", 8),
    "container_network_transmit_bytes_total": ("tx_bits_per_s", 8),
    "container_network_receive_packets_total": ("rx_packets_per_s", 1),
    "container_network_receive_packets_dropped_total": ("rx_drops_per_s", 1),
    "container_network_receive_errors_total": ("rx_errors_per_s", 1),
    "container_network_transmit_packets_dropped_total": ("tx_drops_per_s", 1),
    "container_network_transmit_errors_total": ("tx_errors_per_s", 1),
}

# Kubernetes object names (DNS-1123, plus dots for FQDN node names).
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$")


def _clamp_window(minutes: int | None) -> int:
    if minutes is None:
        return 5
    return max(_MIN_WINDOW, min(_MAX_WINDOW, int(minutes)))


def _short(hostname: str) -> str:
    return hostname.split(".", 1)[0].lower()


def _check_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise PrometheusQueryError(f"Invalid {kind} name: {value!r}")
    return value


def _q(value: str) -> str:
    return json.dumps(value)


class PrometheusProvider:
    name: ClassVar[str] = "prometheus"

    def __init__(self, settings: PrometheusSettings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.base_url)

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._base}/-/ready")
            if resp.status_code == 200:
                return ProviderHealth(name=self.name, status=ProviderStatus.OK)
            return ProviderHealth(
                name=self.name, status=ProviderStatus.UNAVAILABLE, detail=f"status {resp.status_code}"
            )
        except httpx.HTTPError as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))

    @property
    def _base(self) -> str:
        return (self._settings.base_url or "").rstrip("/")

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise PrometheusUnconfiguredError("Prometheus is not configured (set PROMETHEUS_BASE_URL).")

    async def _vector(self, promql: str) -> list[tuple[dict[str, str], float]]:
        """An instant query, as (labels, value) pairs."""
        try:
            async with httpx.AsyncClient(timeout=self._settings.timeout_seconds) as client:
                resp = await client.get(f"{self._base}/api/v1/query", params={"query": promql})
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise PrometheusTimeoutError(f"Timeout querying Prometheus: {exc}") from exc
        except httpx.HTTPError as exc:
            raise PrometheusUnavailableError(f"Prometheus unreachable: {exc}") from exc
        try:
            if payload["status"] != "success":
                raise PrometheusQueryError(f"Prometheus query failed: {payload.get('error')}")
            result = payload["data"]["result"]
            return [(r.get("metric", {}), float(r["value"][1])) for r in result]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PrometheusQueryError(f"Unexpected Prometheus response shape: {exc}") from exc

    async def query(self, promql: str) -> list[tuple[dict[str, str], float]]:
        """An instant PromQL query, as (labels, value) pairs."""
        self._require_configured()
        return await self._vector(promql)

    async def _scalar(self, promql: str) -> float | None:
        rows = await self._vector(promql)
        return rows[0][1] if rows else None

    async def resolve_node_instance(self, node: str) -> tuple[str, str]:
        """(nodename as node-exporter reports it, its scrape instance) for a Kubernetes node name.

        node-exporter reports the host's own hostname, which may be the short
        name where Kubernetes has the FQDN (plk8sdadom001 vs
        plk8sdadom001.lnf.infn.it), so both are compared by their first label.
        """
        self._require_configured()
        _check_name("node", node)
        rows = await self._vector("node_uname_info")
        wanted = _short(node)
        for labels, _ in rows:
            nodename = labels.get("nodename", "")
            if nodename.lower() == node.lower() or _short(nodename) == wanted:
                return nodename, labels.get("instance", "")
        raise PrometheusNodeNotFoundError(f"No node-exporter reports a node named '{node}'.")

    async def node_network(self, node: str, window_minutes: int | None = None) -> NodeNetworkStats:
        nodename, instance = await self.resolve_node_instance(node)
        window = _clamp_window(window_minutes)
        sel = f"instance={_q(instance)}"
        dev_sel = f'{sel}, device!~"{_EXCLUDED_DEVICES}"'

        rate_queries = {
            metric: f"rate({metric}{{{dev_sel}}}[{window}m])" for metric in _NODE_INTERFACE_RATES
        }
        gauges = {
            "speed": f"node_network_speed_bytes{{{dev_sel}}}",
            "mtu": f"node_network_mtu_bytes{{{dev_sel}}}",
            "up": f"node_network_up{{{dev_sel}}}",
        }
        scalars = {
            "udp_rcvbuf": f"sum(rate(node_netstat_Udp_RcvbufErrors{{{sel}}}[{window}m]))",
            "udp_in": f"sum(rate(node_netstat_Udp_InErrors{{{sel}}}[{window}m]))",
            "softnet_dropped": f"sum(rate(node_softnet_dropped_total{{{sel}}}[{window}m]))",
            "softnet_squeezed": f"sum(rate(node_softnet_times_squeezed_total{{{sel}}}[{window}m]))",
        }
        names = list(rate_queries) + list(gauges) + list(scalars)
        queries = list(rate_queries.values()) + list(gauges.values()) + list(scalars.values())
        results = dict(zip(names, await asyncio.gather(*(self._vector(q) for q in queries))))

        interfaces: dict[str, InterfaceTraffic] = {}

        def iface(device: str) -> InterfaceTraffic:
            return interfaces.setdefault(device, InterfaceTraffic(device=device))

        for metric, (attr, factor) in _NODE_INTERFACE_RATES.items():
            for labels, value in results[metric]:
                setattr(iface(labels.get("device", "?")), attr, value * factor)
        for labels, value in results["speed"]:
            # -1 (or 0) means the driver doesn't report a speed (virtual interfaces).
            if value > 0:
                iface(labels.get("device", "?")).speed_bits_per_s = value * 8
        for labels, value in results["mtu"]:
            iface(labels.get("device", "?")).mtu = int(value)
        up = {labels.get("device") for labels, value in results["up"] if value == 1}

        # Keep interfaces that are up or carry anything; an unplugged spare NIC is noise.
        kept = [
            i
            for i in interfaces.values()
            if i.device in up or i.rx_bits_per_s or i.tx_bits_per_s or i.rx_drops_per_s
        ]
        kept.sort(key=lambda i: i.rx_bits_per_s, reverse=True)

        def first(rows: list[tuple[dict[str, str], float]]) -> float:
            return rows[0][1] if rows else 0.0

        return NodeNetworkStats(
            node=nodename,
            instance=instance,
            window_minutes=window,
            interfaces=kept,
            udp_rcvbuf_errors_per_s=first(results["udp_rcvbuf"]),
            udp_in_errors_per_s=first(results["udp_in"]),
            softnet_dropped_per_s=first(results["softnet_dropped"]),
            softnet_squeezed_per_s=first(results["softnet_squeezed"]),
        )

    async def node_cpu(self, node: str, window_minutes: int | None = None) -> NodeCpuStats:
        nodename, instance = await self.resolve_node_instance(node)
        window = _clamp_window(window_minutes)
        sel = f"instance={_q(instance)}"
        cpu = "node_cpu_seconds_total"

        def mode(m: str) -> str:
            return f'rate({cpu}{{{sel}, mode="{m}"}}[{window}m])'

        queries = [
            f"count(count by (cpu) ({cpu}{{{sel}}}))",
            f"1 - avg({mode('idle')})",
            f"avg({mode('softirq')})",
            f"avg({mode('iowait')})",
            f"max(1 - {mode('idle')})",
            f"max({mode('softirq')})",
            f"node_load1{{{sel}}}",
            f"node_memory_MemAvailable_bytes{{{sel}}} / node_memory_MemTotal_bytes{{{sel}}}",
        ]
        cpus, busy, softirq, iowait, max_busy, max_softirq, load1, mem = await asyncio.gather(
            *(self._scalar(q) for q in queries)
        )
        return NodeCpuStats(
            node=nodename,
            window_minutes=window,
            cpus=int(cpus or 0),
            busy_percent=round((busy or 0.0) * 100, 1),
            softirq_percent=round((softirq or 0.0) * 100, 1),
            iowait_percent=round((iowait or 0.0) * 100, 1),
            max_cpu_busy_percent=round((max_busy or 0.0) * 100, 1),
            max_cpu_softirq_percent=round((max_softirq or 0.0) * 100, 1),
            load1=load1,
            memory_available_percent=None if mem is None else round(mem * 100, 1),
        )

    async def pod_network(self, namespace: str, pod: str, window_minutes: int | None = None) -> PodNetworkStats:
        self._require_configured()
        _check_name("namespace", namespace)
        _check_name("pod", pod)
        window = _clamp_window(window_minutes)
        sel = f"namespace={_q(namespace)}, pod={_q(pod)}, interface!~\"{_EXCLUDED_DEVICES}\""

        # max, not sum: the same pod series can be scraped through more than one
        # kubelet endpoint, and summing would double the rate.
        metrics = list(_POD_INTERFACE_RATES)
        results = await asyncio.gather(
            *(self._vector(f"max by (interface, node) (rate({m}{{{sel}}}[{window}m]))") for m in metrics)
        )
        interfaces: dict[str, InterfaceTraffic] = {}
        node: str | None = None
        for metric, rows in zip(metrics, results):
            attr, factor = _POD_INTERFACE_RATES[metric]
            for labels, value in rows:
                device = labels.get("interface", "?")
                node = node or labels.get("node")
                traffic = interfaces.setdefault(device, InterfaceTraffic(device=device))
                setattr(traffic, attr, value * factor)
        return PodNetworkStats(
            namespace=namespace,
            pod=pod,
            window_minutes=window,
            node=node,
            interfaces=sorted(interfaces.values(), key=lambda i: i.device),
        )

