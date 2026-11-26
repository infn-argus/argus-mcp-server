from __future__ import annotations

import pytest

from argus.providers.kubernetes.exceptions import KubernetesResourceNotFoundError, KubernetesUnavailableError
from argus.providers.kubernetes.models import (
    ContainerDetail,
    KubeEvent,
    NodeInfo,
    NodePod,
    PodDetail,
    PodNetworkAttachment,
    PodStatus,
)
from argus.providers.prometheus.models import InterfaceTraffic, NodeCpuStats, NodeNetworkStats, PodNetworkStats
from argus.services.cluster_service import ClusterService

NODE = "plk8sdadom003.lnf.infn.it"


def _pod(**overrides) -> PodDetail:
    base = dict(
        name="sbncamera-03-0",
        namespace="sparc",
        phase="Running",
        ready=True,
        node=NODE,
        pod_ip="10.42.30.20",
        host_ip="192.168.108.14",
        host_network=False,
        started_at=None,
        owner="StatefulSet/sbncamera-03",
        containers=[ContainerDetail(name="ioc", image="img", ready=True, restart_count=0, state="running")],
        networks=[PodNetworkAttachment(name="sparc/sparc-br-cams", interface="net1", ips=["192.168.110.212"])],
    )
    base.update(overrides)
    return PodDetail(**base)


def _node(**overrides) -> NodeInfo:
    base = dict(
        name=NODE, ready=True, unschedulable=False, internal_ip="192.168.108.14",
        kubelet_version="v1.33", kernel_version="6.8", os_image="Ubuntu",
    )
    base.update(overrides)
    return NodeInfo(**base)


class FakeKube:
    def __init__(self, *, pod=None, node=None, pods=None, events=None, node_error=None, ioc_pods=None):
        self.pod, self.node, self.pods, self.events = pod or _pod(), node or _node(), pods or [], events or []
        self.node_error = node_error
        self.ioc_pods = ioc_pods or {}
        self.selectors: list[str | None] = []

    async def get_pod(self, name, namespace):
        return self.pod

    async def list_events(self, namespace, *, object_name=None, limit=30):
        return self.events

    async def get_node(self, name):
        if self.node_error:
            raise self.node_error
        return self.node

    async def list_pods_on_node(self, name):
        if self.node_error:
            raise self.node_error
        return self.pods

    async def get_pod_for_ioc(self, ioc, namespace):
        self.selectors.append("configured")
        return None

    async def list_iocs(self, namespace=None, *, label_selector=None):
        self.selectors.append(label_selector)
        return self.ioc_pods.get(label_selector, [])


class FakeProm:
    def __init__(self, *, configured=True, pod_net=None, node_net=None, cpu=None):
        self.configured = configured
        self.pod_net = pod_net or PodNetworkStats(namespace="sparc", pod="sbncamera-03-0", window_minutes=5)
        self.node_net = node_net or NodeNetworkStats(node=NODE, instance="i", window_minutes=5)
        self.cpu = cpu or NodeCpuStats(
            node=NODE, window_minutes=5, cpus=32, busy_percent=10, softirq_percent=1, iowait_percent=0,
            max_cpu_busy_percent=20, max_cpu_softirq_percent=5, memory_available_percent=60,
        )

    def is_configured(self):
        return self.configured

    async def pod_network(self, namespace, pod, window):
        return self.pod_net

    async def node_network(self, node, window):
        return self.node_net

    async def node_cpu(self, node, window):
        return self.cpu


def _service(kube=None, prom=None) -> ClusterService:
    return ClusterService(kubernetes=kube or FakeKube(), prometheus=prom or FakeProm(), default_namespace="sparc")


def _messages(report, severity=None):
    return [f["message"] for f in report.to_dict()["findings"] if severity is None or f["severity"] == severity]


async def test_healthy_pod_has_no_findings_and_lists_sources():
    report = (await _service().inspect_pod("sbncamera-03-0", None)).to_dict()
    assert report["subject"] == "pod sparc/sbncamera-03-0"
    assert report["findings"] == []
    assert report["pod"]["networks"][0]["interface"] == "net1"
    assert report["node"]["name"] == NODE
    assert set(report["sources"]) == {"pod", "events", "pod_network", "node"}
    assert all(v == "ok" for v in report["sources"].values())


async def test_pod_rules_crashloop_oom_events_and_net1_drops():
    pod = _pod(
        ready=False,
        conditions_not_ok=["ContainersReady", "Ready"],
        containers=[
            ContainerDetail(
                name="ioc", image="img", ready=False, restart_count=4,
                state="waiting: CrashLoopBackOff", last_termination="OOMKilled (exit 137) at 2026-11-26",
            )
        ],
    )
    events = [KubeEvent(type="Warning", reason="BackOff", message="Back-off restarting", count=3, last_seen=None, object="Pod/x")]
    net = PodNetworkStats(
        namespace="sparc", pod="sbncamera-03-0", window_minutes=5,
        interfaces=[InterfaceTraffic(device="net1", rx_bits_per_s=200e6, rx_drops_per_s=120.0)],
    )
    report = await _service(FakeKube(pod=pod, events=events), FakeProm(pod_net=net)).inspect_pod("sbncamera-03-0", "sparc")

    critical = _messages(report, "critical")
    warnings = _messages(report, "warning")
    assert any("CrashLoopBackOff" in m for m in critical)
    assert any("OOMKilled" in m for m in warnings)
    assert any("BackOff" in m for m in warnings)
    assert any("net1 drops 120 pkt/s" in m and "200 Mbit/s" in m for m in warnings)
    # critical first
    assert report.to_dict()["findings"][0]["severity"] == "critical"


async def test_pod_without_prometheus_still_reports_kubernetes():
    report = (await _service(prom=FakeProm(configured=False)).inspect_pod("sbncamera-03-0", "sparc")).to_dict()
    assert report["sources"]["pod_network"].startswith("skipped")
    assert "pod" in report and "pod_network" not in report


async def test_bond_only_drops_are_info_vlan_drops_are_warning():
    bond_only = NodeNetworkStats(
        node=NODE, instance="i", window_minutes=5,
        interfaces=[
            InterfaceTraffic(device="bond0", rx_drops_per_s=1055.0, speed_bits_per_s=20e9),
            InterfaceTraffic(device="vlan110", rx_drops_per_s=0.0),
        ],
    )
    report = await _service(prom=FakeProm(node_net=bond_only)).inspect_node(NODE)
    assert any("bond0 1055/s" in m and "VLANs" in m for m in _messages(report, "info"))
    assert _messages(report, "warning") == []

    vlan = NodeNetworkStats(
        node=NODE, instance="i", window_minutes=5,
        interfaces=[InterfaceTraffic(device="vlan110", rx_drops_per_s=40.0), InterfaceTraffic(device="br110", rx_drops_per_s=0)],
    )
    report = await _service(prom=FakeProm(node_net=vlan)).inspect_node(NODE)
    assert any("Receive drops on vlan110 40/s" in m for m in _messages(report, "warning"))


async def test_node_rules_saturation_udp_backlog_softirq_memory():
    net = NodeNetworkStats(
        node=NODE, instance="i", window_minutes=5,
        interfaces=[InterfaceTraffic(device="eno1", rx_bits_per_s=900e6, speed_bits_per_s=1e9, rx_errors_per_s=2)],
        udp_rcvbuf_errors_per_s=3.0,
        softnet_dropped_per_s=5.0,
    )
    cpu = NodeCpuStats(
        node=NODE, window_minutes=5, cpus=8, busy_percent=95, softirq_percent=12, iowait_percent=0,
        max_cpu_busy_percent=100, max_cpu_softirq_percent=85, memory_available_percent=5,
    )
    report = await _service(prom=FakeProm(node_net=net, cpu=cpu)).inspect_node(NODE)
    joined = " | ".join(_messages(report, "warning"))
    for expected in ("over 80% of its 1000 Mbit/s", "receive errors", "UDP receive-buffer overflows", "backlog drops",
                     "95% busy", "85% in softirq", "5% of memory"):
        assert expected in joined, expected


async def test_node_report_lists_pods_on_extra_networks_and_not_ready():
    pods = [
        NodePod(namespace="sparc", name="sbncamera-03-0", phase="Running", ready=True, restart_count=0,
                extra_networks=["sparc/sparc-br-cams"]),
        NodePod(namespace="btf", name="broken-0", phase="Pending", ready=False, restart_count=0),
        NodePod(namespace="sparc", name="job-x", phase="Succeeded", ready=False, restart_count=0),
    ]
    report = (await _service(FakeKube(pods=pods)).inspect_node(NODE)).to_dict()
    assert report["pods"]["total"] == 3
    assert report["pods"]["by_namespace"] == {"btf": 1, "sparc": 2}
    assert report["pods"]["on_extra_networks"] == [
        {"pod": "sparc/sbncamera-03-0", "networks": ["sparc/sparc-br-cams"], "ready": True}
    ]
    assert report["pods"]["not_ready"] == ["btf/broken-0 (Pending)"]
    assert any("btf/broken-0" in f["message"] for f in report["findings"])


async def test_node_forbidden_degrades_to_prometheus_only():
    kube = FakeKube(node_error=KubernetesUnavailableError("nodes is forbidden"))
    report = (await _service(kube).inspect_node(NODE)).to_dict()
    assert report["sources"]["node"].startswith("unavailable") and "forbidden" in report["sources"]["node"]
    assert "network" in report and "cpu" in report and "node" not in report


async def test_node_problems_are_critical():
    report = await _service(FakeKube(node=_node(ready=False, problems=["Ready=False"]))).inspect_node(NODE)
    assert _messages(report, "critical") == ["Node has Ready=False."]


async def test_resolve_ioc_pod_falls_back_to_epik8s_label():
    pod = PodStatus(name="sbncamera-03-0", namespace="sparc", phase="Running", ready=True, restart_count=0)
    kube = FakeKube(ioc_pods={"app.kubernetes.io/instance=sbncamera-03": [pod]})
    assert await _service(kube).resolve_ioc_pod("sbncamera-03", None) == "sbncamera-03-0"
    assert kube.selectors == ["configured", "app.kubernetes.io/instance=sbncamera-03"]


async def test_resolve_unknown_ioc_raises():
    with pytest.raises(KubernetesResourceNotFoundError):
        await _service().resolve_ioc_pod("nosuch", "sparc")

