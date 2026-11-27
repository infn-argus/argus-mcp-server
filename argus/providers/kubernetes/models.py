from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PodStatus:
    name: str
    namespace: str
    phase: str
    ready: bool
    restart_count: int
    node: str | None = None
    started_at: datetime | None = None


@dataclass
class PodNetworkAttachment:
    """One network the pod is attached to, as Multus reports it (k8s.v1.cni.cncf.io/network-status)."""

    name: str
    interface: str | None = None
    ips: list[str] = field(default_factory=list)
    mac: str | None = None
    default: bool = False


@dataclass
class ContainerDetail:
    name: str
    image: str
    ready: bool
    restart_count: int
    # "running", "waiting: CrashLoopBackOff", "terminated: Error (exit 1)"
    state: str
    # Why the previous instance ended, when it restarted: "OOMKilled (exit 137) at ...".
    last_termination: str | None = None


@dataclass
class PodDetail:
    name: str
    namespace: str
    phase: str
    ready: bool
    node: str | None
    pod_ip: str | None
    host_ip: str | None
    host_network: bool
    started_at: datetime | None
    owner: str | None
    containers: list[ContainerDetail] = field(default_factory=list)
    networks: list[PodNetworkAttachment] = field(default_factory=list)
    # Where the pod may run: nodeSelector entries and required node-affinity terms.
    placement: list[str] = field(default_factory=list)
    conditions_not_ok: list[str] = field(default_factory=list)


@dataclass
class KubeEvent:
    type: str
    reason: str
    message: str
    count: int
    last_seen: datetime | None
    object: str


@dataclass
class NodePod:
    namespace: str
    name: str
    phase: str
    ready: bool
    restart_count: int
    # Extra (non-default) Multus networks, e.g. ["sparc/sparc-br-cams"].
    extra_networks: list[str] = field(default_factory=list)


@dataclass
class NodeInfo:
    name: str
    ready: bool
    unschedulable: bool
    internal_ip: str | None
    kubelet_version: str | None
    kernel_version: str | None
    os_image: str | None
    # Conditions in a bad state: Ready!=True, or any *Pressure / NetworkUnavailable == True.
    problems: list[str] = field(default_factory=list)
    taints: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    capacity: dict[str, str] = field(default_factory=dict)
    allocatable: dict[str, str] = field(default_factory=dict)


@dataclass
class IocWorkload:
    """A StatefulSet/Deployment rendered by epik8s' ioc-chart (label app.kubernetes.io/name=ioc-chart)."""

    kind: str  # "StatefulSet" | "Deployment"
    name: str
    namespace: str
    instance: str  # app.kubernetes.io/instance: the IOC name it was deployed as
    replicas: int
    ready_replicas: int
    created: datetime | None
    # ArgoCD's tracking-id annotation "<app>:<group>/<kind>:<ns>/<name>": the
    # Application that manages it. None = not deployed by ArgoCD (kubectl/helm by hand).
    argocd_app: str | None = None
