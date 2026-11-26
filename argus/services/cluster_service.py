"""Cluster diagnostics: a pod's or a node's health, with the findings spelled out.

Combines the Kubernetes API (pod detail, Multus networks, events, node
conditions, what else runs on the node) with Prometheus (node-exporter and
cAdvisor rates), and applies a few rules that turn numbers into statements an
operator can act on - "net1 drops 120 pkt/s", "UDP receive buffers overflow",
"one CPU is 85% in softirq". Each backend is optional: a missing one is
reported under ``sources`` and the rest of the report still comes back.

Read-only. Rates are over a short window (default 5 minutes), so a report
describes what is happening now, not a cumulative counter since boot.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from argus.core.models import ProviderResult, gather_with_timeout, skipped
from argus.providers.kubernetes.exceptions import KubernetesResourceNotFoundError
from argus.providers.kubernetes.kubernetes import KubernetesProvider
from argus.providers.kubernetes.models import KubeEvent, NodeInfo, NodePod, PodDetail
from argus.providers.prometheus.models import NodeCpuStats, NodeNetworkStats, PodNetworkStats
from argus.providers.prometheus.prometheus import PrometheusProvider

_TIMEOUT = 15.0

# Thresholds for findings. Deliberately few and conservative: a finding should
# be worth reading, and the raw numbers are in the report for anything finer.
LINK_SATURATION_FRACTION = 0.8
DROPS_PER_S_WARN = 1.0
UDP_RCVBUF_ERRORS_PER_S_WARN = 0.1
SOFTNET_SQUEEZED_PER_S_INFO = 100.0
CPU_SOFTIRQ_PERCENT_WARN = 50.0
CPU_BUSY_PERCENT_WARN = 90.0
MEMORY_AVAILABLE_PERCENT_WARN = 10.0

# epik8s' ioc-chart labels an IOC's pods with its name here; tried when the
# configured K8S_IOC_LABEL_SELECTOR_TEMPLATE finds nothing.
EPIK8S_IOC_LABEL = "app.kubernetes.io/instance"


@dataclass
class Finding:
    severity: str  # "critical" | "warning" | "info"
    source: str  # "kubernetes" | "prometheus"
    message: str


@dataclass
class ClusterReport:
    subject: str
    window_minutes: int
    findings: list[Finding] = field(default_factory=list)
    sections: dict[str, Any] = field(default_factory=dict)
    # Per backend call: "ok", or why it is missing (unconfigured, forbidden, timeout...).
    sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        order = {"critical": 0, "warning": 1, "info": 2}
        findings = sorted(self.findings, key=lambda f: order.get(f.severity, 3))
        return {
            "subject": self.subject,
            "window_minutes": self.window_minutes,
            "findings": [dataclasses.asdict(f) for f in findings],
            **self.sections,
            "sources": self.sources,
        }


def _mbit(bits: float) -> str:
    return f"{bits / 1e6:.0f} Mbit/s"


def _record(report: ClusterReport, results: dict[str, ProviderResult]) -> None:
    for key, result in results.items():
        report.sources[key] = "ok" if result.ok else f"{result.status}: {result.error}"


class ClusterService:
    def __init__(
        self, *, kubernetes: KubernetesProvider, prometheus: PrometheusProvider, default_namespace: str = "default"
    ) -> None:
        self._kubernetes = kubernetes
        self._prometheus = prometheus
        self._default_namespace = default_namespace

    async def resolve_ioc_pod(self, ioc: str, namespace: str | None) -> str:
        namespace = namespace or self._default_namespace
        pod = await self._kubernetes.get_pod_for_ioc(ioc, namespace)
        if pod is None:
            pods = await self._kubernetes.list_iocs(namespace=namespace, label_selector=f"{EPIK8S_IOC_LABEL}={ioc}")
            pod = pods[0] if pods else None
        if pod is None:
            raise KubernetesResourceNotFoundError(f"No pod found for IOC '{ioc}'.")
        return pod.name

    # ---- pod ---------------------------------------------------------------

    async def inspect_pod(self, pod: str, namespace: str | None, *, window_minutes: int = 5) -> ClusterReport:
        namespace = namespace or self._default_namespace
        report = ClusterReport(subject=f"pod {namespace}/{pod}", window_minutes=window_minutes)
        tasks: dict[str, Awaitable[Any]] = {
            "pod": self._kubernetes.get_pod(pod, namespace),
            "events": self._kubernetes.list_events(namespace, object_name=pod, limit=15),
        }
        if self._prometheus.is_configured():
            tasks["pod_network"] = self._prometheus.pod_network(namespace, pod, window_minutes)
        results: dict[str, ProviderResult] = await gather_with_timeout(tasks, timeout=_TIMEOUT)
        if "pod_network" not in results:
            results["pod_network"] = skipped("prometheus", "PROMETHEUS_BASE_URL not set")

        detail: PodDetail | None = results["pod"].data
        if detail and detail.node:
            node_results = await gather_with_timeout(
                {"node": self._kubernetes.get_node(detail.node)}, timeout=_TIMEOUT
            )
            results.update(node_results)
        _record(report, results)

        if detail is not None:
            report.sections["pod"] = dataclasses.asdict(detail)
            report.findings += _pod_findings(detail)
        events: list[KubeEvent] = results["events"].data or []
        report.sections["events"] = [dataclasses.asdict(e) for e in events]
        report.findings += _event_findings(events)
        net: PodNetworkStats | None = results["pod_network"].data
        if net is not None:
            report.sections["pod_network"] = dataclasses.asdict(net)
            report.findings += _pod_network_findings(net)
        node_result = results.get("node")
        node: NodeInfo | None = node_result.data if node_result else None
        if node is not None:
            report.sections["node"] = {
                "name": node.name,
                "ready": node.ready,
                "problems": node.problems,
                "internal_ip": node.internal_ip,
            }
            report.findings += [
                Finding("critical", "kubernetes", f"Node {node.name} has {p}.") for p in node.problems
            ]
        return report

    # ---- node --------------------------------------------------------------

    async def inspect_node(self, node: str, *, window_minutes: int = 5) -> ClusterReport:
        report = ClusterReport(subject=f"node {node}", window_minutes=window_minutes)
        tasks: dict[str, Awaitable[Any]] = {
            "node": self._kubernetes.get_node(node),
            "pods": self._kubernetes.list_pods_on_node(node),
        }
        if self._prometheus.is_configured():
            tasks["network"] = self._prometheus.node_network(node, window_minutes)
            tasks["cpu"] = self._prometheus.node_cpu(node, window_minutes)
        results: dict[str, ProviderResult] = await gather_with_timeout(tasks, timeout=_TIMEOUT)
        for key in ("network", "cpu"):
            results.setdefault(key, skipped("prometheus", "PROMETHEUS_BASE_URL not set"))
        _record(report, results)

        info: NodeInfo | None = results["node"].data
        if info is not None:
            report.sections["node"] = dataclasses.asdict(info)
            report.findings += _node_findings(info)
        pods: list[NodePod] | None = results["pods"].data
        if pods is not None:
            report.sections["pods"] = _pods_summary(pods)
            report.findings += _node_pods_findings(pods)
        net: NodeNetworkStats | None = results["network"].data
        if net is not None:
            report.sections["network"] = dataclasses.asdict(net)
            report.findings += _node_network_findings(net)
        cpu: NodeCpuStats | None = results["cpu"].data
        if cpu is not None:
            report.sections["cpu"] = dataclasses.asdict(cpu)
            report.findings += _cpu_findings(cpu)
        return report


# ---- rules ---------------------------------------------------------------


def _pod_findings(pod: PodDetail) -> list[Finding]:
    out: list[Finding] = []
    if pod.phase != "Running":
        out.append(Finding("critical", "kubernetes", f"Pod is {pod.phase}, not Running."))
    elif not pod.ready:
        out.append(Finding("warning", "kubernetes", f"Pod is Running but not Ready ({', '.join(pod.conditions_not_ok)})."))
    for c in pod.containers:
        if c.state.startswith("waiting: CrashLoopBackOff"):
            out.append(Finding("critical", "kubernetes", f"Container {c.name} is in CrashLoopBackOff."))
        if c.restart_count and c.last_termination:
            severity = "warning" if "OOMKilled" in c.last_termination else "info"
            out.append(
                Finding(severity, "kubernetes", f"Container {c.name} restarted {c.restart_count}x; last: {c.last_termination}.")
            )
    return out


def _event_findings(events: list[KubeEvent]) -> list[Finding]:
    warnings = [e for e in events if e.type == "Warning"]
    if not warnings:
        return []
    reasons = sorted({e.reason for e in warnings})
    return [
        Finding("warning", "kubernetes", f"{len(warnings)} recent Warning event(s): {', '.join(reasons)}. Latest: {warnings[0].message[:200]}")
    ]


def _pod_network_findings(net: PodNetworkStats) -> list[Finding]:
    out = []
    for i in net.interfaces:
        if i.rx_drops_per_s >= DROPS_PER_S_WARN:
            out.append(
                Finding(
                    "warning",
                    "prometheus",
                    f"Pod interface {i.device} drops {i.rx_drops_per_s:.0f} pkt/s on receive (at {_mbit(i.rx_bits_per_s)}): "
                    "the pod does not read fast enough - socket buffers (net.core.rmem_max) or CPU.",
                )
            )
        if i.rx_errors_per_s > 0:
            out.append(Finding("warning", "prometheus", f"Pod interface {i.device} has {i.rx_errors_per_s:.1f} receive errors/s."))
    return out


def _node_findings(node: NodeInfo) -> list[Finding]:
    out = [Finding("critical", "kubernetes", f"Node has {p}.") for p in node.problems]
    if node.unschedulable:
        out.append(Finding("info", "kubernetes", "Node is cordoned (unschedulable)."))
    return out


def _pods_summary(pods: list[NodePod]) -> dict[str, Any]:
    by_namespace: dict[str, int] = {}
    for p in pods:
        by_namespace[p.namespace] = by_namespace.get(p.namespace, 0) + 1
    return {
        "total": len(pods),
        "by_namespace": dict(sorted(by_namespace.items())),
        # Pods on extra Multus networks (camera VLANs, instrument networks): these
        # share the node's VLAN uplink, which is what decides a bandwidth problem.
        "on_extra_networks": [
            {"pod": f"{p.namespace}/{p.name}", "networks": p.extra_networks, "ready": p.ready}
            for p in pods
            if p.extra_networks
        ],
        "not_ready": [f"{p.namespace}/{p.name} ({p.phase})" for p in pods if not p.ready and p.phase != "Succeeded"],
    }


def _node_pods_findings(pods: list[NodePod]) -> list[Finding]:
    bad = [p for p in pods if not p.ready and p.phase not in ("Succeeded",)]
    if not bad:
        return []
    names = ", ".join(f"{p.namespace}/{p.name}" for p in bad[:8])
    return [Finding("warning", "kubernetes", f"{len(bad)} pod(s) on this node not Ready: {names}.")]


def _node_network_findings(net: NodeNetworkStats) -> list[Finding]:
    out = []
    dropping = [i for i in net.interfaces if i.rx_drops_per_s >= DROPS_PER_S_WARN]
    for i in net.interfaces:
        if i.speed_bits_per_s and i.rx_bits_per_s >= LINK_SATURATION_FRACTION * i.speed_bits_per_s:
            out.append(
                Finding("warning", "prometheus", f"{i.device} receives {_mbit(i.rx_bits_per_s)}, over 80% of its {_mbit(i.speed_bits_per_s)} link.")
            )
        if i.speed_bits_per_s and i.tx_bits_per_s >= LINK_SATURATION_FRACTION * i.speed_bits_per_s:
            out.append(
                Finding("warning", "prometheus", f"{i.device} sends {_mbit(i.tx_bits_per_s)}, over 80% of its {_mbit(i.speed_bits_per_s)} link.")
            )
        if i.rx_errors_per_s > 0:
            out.append(Finding("warning", "prometheus", f"{i.device} has {i.rx_errors_per_s:.1f} receive errors/s (cable, optic or NIC)."))
    if dropping:
        names = {i.device for i in dropping}
        detail = ", ".join(f"{i.device} {i.rx_drops_per_s:.0f}/s" for i in dropping)
        # A bond/NIC dropping while its VLAN and bridge interfaces don't is usually
        # the kernel discarding frames for VLANs or protocols this node doesn't
        # carry - noisy but harmless. Say which case it is.
        upper_only = all(not (n.startswith("vlan") or n.startswith("br") or "." in n) for n in names)
        if upper_only:
            out.append(
                Finding(
                    "info",
                    "prometheus",
                    f"Receive drops on {detail}, but none on the VLAN/bridge interfaces: usually frames for VLANs or "
                    "protocols this node does not carry, not lost application traffic.",
                )
            )
        else:
            out.append(Finding("warning", "prometheus", f"Receive drops on {detail}."))
    if net.udp_rcvbuf_errors_per_s >= UDP_RCVBUF_ERRORS_PER_S_WARN:
        out.append(
            Finding(
                "warning",
                "prometheus",
                f"UDP receive-buffer overflows: {net.udp_rcvbuf_errors_per_s:.1f}/s on the host network namespace - a UDP "
                "receiver is too slow, or net.core.rmem_max too small. (Pods have their own counters, see inspect_pod.)",
            )
        )
    if net.softnet_dropped_per_s > 0:
        out.append(
            Finding("warning", "prometheus", f"Kernel backlog drops: {net.softnet_dropped_per_s:.1f} pkt/s (raise net.core.netdev_max_backlog, or spread IRQs).")
        )
    if net.softnet_squeezed_per_s >= SOFTNET_SQUEEZED_PER_S_INFO:
        out.append(
            Finding("info", "prometheus", f"Packet processing runs out of budget {net.softnet_squeezed_per_s:.0f}x/s (net.core.netdev_budget); fine unless drops appear.")
        )
    return out


def _cpu_findings(cpu: NodeCpuStats) -> list[Finding]:
    out = []
    if cpu.busy_percent >= CPU_BUSY_PERCENT_WARN:
        out.append(Finding("warning", "prometheus", f"Node CPU is {cpu.busy_percent:.0f}% busy on average."))
    if cpu.max_cpu_softirq_percent >= CPU_SOFTIRQ_PERCENT_WARN:
        out.append(
            Finding(
                "warning",
                "prometheus",
                f"One CPU spends {cpu.max_cpu_softirq_percent:.0f}% in softirq (packet processing) while the node averages "
                f"{cpu.softirq_percent:.0f}%: receive traffic is pinned to one core (check RSS/IRQ affinity).",
            )
        )
    if cpu.memory_available_percent is not None and cpu.memory_available_percent < MEMORY_AVAILABLE_PERCENT_WARN:
        out.append(Finding("warning", "prometheus", f"Only {cpu.memory_available_percent:.0f}% of memory available."))
    return out
