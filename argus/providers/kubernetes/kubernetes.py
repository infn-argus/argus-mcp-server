"""Kubernetes provider, built on the official ``kubernetes`` client.

The official client is synchronous, so every call is offloaded to a thread via
``asyncio.to_thread`` rather than pulling in a separate async k8s library —
simplest option that still satisfies the async requirement.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, ClassVar

from argus.config.settings import KubernetesSettings
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.kubernetes.exceptions import (
    KubernetesResourceNotFoundError,
    KubernetesUnavailableError,
    KubernetesUnconfiguredError,
)
from argus.providers.kubernetes.models import (
    ContainerDetail,
    KubeEvent,
    NodeInfo,
    NodePod,
    PodDetail,
    PodNetworkAttachment,
    PodStatus,
)

# Multus records every network a pod got, with its interface and IPs, here.
NETWORK_STATUS_ANNOTATION = "k8s.v1.cni.cncf.io/network-status"

# Node labels that describe hardware features or are duplicates of others:
# hundreds of feature.node.kubernetes.io/* flags would bury the ones that
# decide placement (kubernetes.io/hostname, control=..., roles).
_NOISY_LABEL_PREFIXES = ("feature.node.kubernetes.io/", "beta.kubernetes.io/", "nvidia.com/")

# DNS-1123 names (dots allowed for FQDN node names): checked before a name goes
# into a field selector, so a tool argument can't add selector terms.
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$")


def _check_name(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise KubernetesResourceNotFoundError(f"Invalid {kind} name: {value!r}")
    return value


class KubernetesProvider:
    name: ClassVar[str] = "kubernetes"

    def __init__(self, settings: KubernetesSettings) -> None:
        self._settings = settings
        self._configured = self._try_load_config()
        self._api: Any | None = None

    def _try_load_config(self) -> bool:
        try:
            from kubernetes import config

            if self._settings.kubeconfig_path:
                config.load_kube_config(config_file=self._settings.kubeconfig_path)
            else:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config()
            return True
        except Exception:  # noqa: BLE001 - any failure here just means "unconfigured"
            return False

    def is_configured(self) -> bool:
        return self._configured

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            await asyncio.to_thread(self._core_v1().get_api_resources)
            return ProviderHealth(name=self.name, status=ProviderStatus.OK)
        except Exception as exc:  # noqa: BLE001
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise KubernetesUnconfiguredError(
                "Kubernetes is not configured (no KUBECONFIG and not running in-cluster)."
            )

    def _core_v1(self) -> Any:
        if self._api is None:
            from kubernetes.client import CoreV1Api

            self._api = CoreV1Api()
        return self._api

    def _namespace_or_default(self, namespace: str | None) -> str:
        return namespace or self._settings.default_namespace

    async def get_pod_for_ioc(self, ioc_name: str, namespace: str | None = None) -> PodStatus | None:
        self._require_configured()
        ns = self._namespace_or_default(namespace)
        selector = self._settings.ioc_label_selector_template.format(ioc_name=ioc_name)
        pods = await self.list_iocs(namespace=ns, label_selector=selector)
        return pods[0] if pods else None

    async def list_iocs(self, namespace: str | None = None, *, label_selector: str | None = None) -> list[PodStatus]:
        self._require_configured()
        ns = self._namespace_or_default(namespace)
        try:
            result = await asyncio.to_thread(
                self._core_v1().list_namespaced_pod, ns, label_selector=label_selector
            )
        except Exception as exc:  # noqa: BLE001
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc

        return [_to_pod_status(item) for item in result.items]

    async def get_pod_logs(self, pod_name: str, namespace: str, tail_lines: int = 200) -> str:
        self._require_configured()
        try:
            return await asyncio.to_thread(
                self._core_v1().read_namespaced_pod_log, pod_name, namespace, tail_lines=tail_lines
            )
        except Exception as exc:  # noqa: BLE001
            raise KubernetesResourceNotFoundError(f"Could not read logs for pod '{pod_name}': {exc}") from exc

    async def restart_pod(self, pod_name: str, namespace: str) -> bool:
        self._require_configured()
        try:
            await asyncio.to_thread(self._core_v1().delete_namespaced_pod, pod_name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise KubernetesResourceNotFoundError(f"Could not restart pod '{pod_name}': {exc}") from exc
        return True

    async def get_pod(self, pod_name: str, namespace: str | None = None) -> PodDetail:
        self._require_configured()
        ns = _check_name("namespace", self._namespace_or_default(namespace))
        _check_name("pod", pod_name)
        try:
            pod = await asyncio.to_thread(self._core_v1().read_namespaced_pod, pod_name, ns)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                raise KubernetesResourceNotFoundError(f"Pod '{pod_name}' not found in '{ns}'.") from exc
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        return _to_pod_detail(pod)

    async def list_events(
        self, namespace: str | None = None, *, object_name: str | None = None, limit: int = 30
    ) -> list[KubeEvent]:
        """Events in a namespace, newest first; only those about ``object_name`` when given."""
        self._require_configured()
        ns = _check_name("namespace", self._namespace_or_default(namespace))
        kwargs: dict[str, Any] = {}
        if object_name:
            kwargs["field_selector"] = f"involvedObject.name={_check_name('object', object_name)}"
        try:
            result = await asyncio.to_thread(self._core_v1().list_namespaced_event, ns, **kwargs)
        except Exception as exc:  # noqa: BLE001
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        events = [_to_event(e) for e in result.items]
        events.sort(key=lambda e: e.last_seen or datetime.min.replace(tzinfo=None), reverse=True)
        return events[: max(1, limit)]

    async def get_node(self, node_name: str) -> NodeInfo:
        self._require_configured()
        _check_name("node", node_name)
        try:
            node = await asyncio.to_thread(self._core_v1().read_node, node_name)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                raise KubernetesResourceNotFoundError(f"Node '{node_name}' not found.") from exc
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        return _to_node_info(node)

    async def list_pods_on_node(self, node_name: str) -> list[NodePod]:
        """Every pod scheduled on a node, across namespaces (needs cluster-wide pod list)."""
        self._require_configured()
        _check_name("node", node_name)
        try:
            result = await asyncio.to_thread(
                self._core_v1().list_pod_for_all_namespaces, field_selector=f"spec.nodeName={node_name}"
            )
        except Exception as exc:  # noqa: BLE001
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        pods = [_to_node_pod(p) for p in result.items]
        pods.sort(key=lambda p: (p.namespace, p.name))
        return pods


def _network_attachments(pod: Any) -> list[PodNetworkAttachment]:
    raw = ((pod.metadata.annotations or {}) if pod.metadata else {}).get(NETWORK_STATUS_ANNOTATION)
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [
        PodNetworkAttachment(
            name=e.get("name", "?"),
            interface=e.get("interface"),
            ips=list(e.get("ips") or []),
            mac=e.get("mac"),
            default=bool(e.get("default", False)),
        )
        for e in entries
        if isinstance(e, dict)
    ]


def _container_state(state: Any) -> str:
    if state is None:
        return "unknown"
    if state.running is not None:
        return "running"
    if state.waiting is not None:
        return f"waiting: {state.waiting.reason or '?'}"
    if state.terminated is not None:
        t = state.terminated
        return f"terminated: {t.reason or '?'} (exit {t.exit_code})"
    return "unknown"


def _last_termination(last_state: Any) -> str | None:
    t = getattr(last_state, "terminated", None) if last_state is not None else None
    if t is None:
        return None
    when = f" at {t.finished_at.isoformat()}" if getattr(t, "finished_at", None) else ""
    return f"{t.reason or '?'} (exit {t.exit_code}){when}"


def _placement(spec: Any) -> list[str]:
    rules = [f"{k}={v}" for k, v in sorted((spec.node_selector or {}).items())]
    affinity = getattr(spec, "affinity", None)
    node_affinity = getattr(affinity, "node_affinity", None) if affinity else None
    required = getattr(node_affinity, "required_during_scheduling_ignored_during_execution", None)
    for term in getattr(required, "node_selector_terms", None) or []:
        for expr in term.match_expressions or []:
            rules.append(f"{expr.key} {expr.operator} {list(expr.values or [])}")
    return rules


def _to_pod_detail(pod: Any) -> PodDetail:
    status, spec, meta = pod.status, pod.spec, pod.metadata
    conditions = {c.type: c.status for c in (status.conditions or [])}
    owners = meta.owner_references or []
    containers = [
        ContainerDetail(
            name=cs.name,
            image=cs.image,
            ready=bool(cs.ready),
            restart_count=cs.restart_count,
            state=_container_state(cs.state),
            last_termination=_last_termination(cs.last_state),
        )
        for cs in (status.container_statuses or [])
    ]
    return PodDetail(
        name=meta.name,
        namespace=meta.namespace,
        phase=status.phase,
        ready=conditions.get("Ready") == "True",
        node=spec.node_name,
        pod_ip=status.pod_ip,
        host_ip=status.host_ip,
        host_network=bool(spec.host_network),
        started_at=status.start_time,
        owner=f"{owners[0].kind}/{owners[0].name}" if owners else None,
        containers=containers,
        networks=_network_attachments(pod),
        placement=_placement(spec),
        conditions_not_ok=sorted(k for k, v in conditions.items() if v != "True"),
    )


def _to_event(event: Any) -> KubeEvent:
    obj = event.involved_object
    last = event.last_timestamp or getattr(event, "event_time", None) or event.metadata.creation_timestamp
    if isinstance(last, datetime) and last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    return KubeEvent(
        type=event.type or "",
        reason=event.reason or "",
        message=(event.message or "").strip(),
        count=event.count or 1,
        last_seen=last,
        object=f"{obj.kind}/{obj.name}" if obj else "",
    )


def _to_node_pod(pod: Any) -> NodePod:
    conditions = {c.type: c.status for c in (pod.status.conditions or [])}
    return NodePod(
        namespace=pod.metadata.namespace,
        name=pod.metadata.name,
        phase=pod.status.phase,
        ready=conditions.get("Ready") == "True",
        restart_count=sum(cs.restart_count for cs in (pod.status.container_statuses or [])),
        extra_networks=[n.name for n in _network_attachments(pod) if not n.default],
    )


def _to_node_info(node: Any) -> NodeInfo:
    status, spec, meta = node.status, node.spec, node.metadata
    conditions = {c.type: c.status for c in (status.conditions or [])}
    problems = []
    if conditions.get("Ready") != "True":
        problems.append(f"Ready={conditions.get('Ready', 'Unknown')}")
    problems += [f"{k}=True" for k, v in conditions.items() if k != "Ready" and v == "True"]
    addresses = {a.type: a.address for a in (status.addresses or [])}
    info = status.node_info
    return NodeInfo(
        name=meta.name,
        ready=conditions.get("Ready") == "True",
        unschedulable=bool(spec.unschedulable),
        internal_ip=addresses.get("InternalIP"),
        kubelet_version=getattr(info, "kubelet_version", None),
        kernel_version=getattr(info, "kernel_version", None),
        os_image=getattr(info, "os_image", None),
        problems=sorted(problems),
        taints=[f"{t.key}={t.value or ''}:{t.effect}" for t in (spec.taints or [])],
        labels={
            k: v for k, v in sorted((meta.labels or {}).items()) if not k.startswith(_NOISY_LABEL_PREFIXES)
        },
        capacity=dict(status.capacity or {}),
        allocatable=dict(status.allocatable or {}),
    )


def _to_pod_status(pod: Any) -> PodStatus:
    status = pod.status
    conditions = {c.type: c.status for c in (status.conditions or [])}
    restart_count = sum(cs.restart_count for cs in (status.container_statuses or []))
    return PodStatus(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        phase=status.phase,
        ready=conditions.get("Ready") == "True",
        restart_count=restart_count,
        node=pod.spec.node_name if pod.spec else None,
        started_at=status.start_time,
    )
