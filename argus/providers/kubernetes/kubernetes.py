"""Kubernetes provider, built on the official ``kubernetes`` client.

The official client is synchronous, so every call is offloaded to a thread via
``asyncio.to_thread`` rather than pulling in a separate async k8s library —
simplest option that still satisfies the async requirement.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
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
    IocWorkload,
    KubeEvent,
    NodeInfo,
    NodePod,
    PodDetail,
    PodNetworkAttachment,
    PodStatus,
)

# Multus records every network a pod got, with its interface and IPs, here.
NETWORK_STATUS_ANNOTATION = "k8s.v1.cni.cncf.io/network-status"

# ioc-chart's chart name label, and ArgoCD's resource-tracking annotation.
IOC_CHART_SELECTOR = "app.kubernetes.io/name=ioc-chart"
ARGOCD_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"

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

    async def exec_in_pod(
        self,
        pod_name: str,
        namespace: str,
        command: list[str],
        *,
        container: str | None = None,
        timeout: float = 30.0,
    ) -> tuple[int | None, str, str]:
        """Run ``command`` (an argv list, never a shell string) in a pod; return (exit code, stdout, stderr).

        Needs ``pods/exec`` create on the namespace. Callers pass fixed
        commands only (e.g. the connectivity probe): nothing here builds a
        command from free text.
        """
        self._require_configured()
        ns = _check_name("namespace", namespace)
        _check_name("pod", pod_name)
        if container is not None:
            _check_name("container", container)
        if not command or not all(isinstance(part, str) for part in command):
            raise KubernetesResourceNotFoundError("exec command must be a non-empty list of strings.")

        def _run() -> tuple[int | None, str, str]:
            from kubernetes.client import ApiClient, CoreV1Api
            from kubernetes.stream import stream

            # stream() swaps the API client's request method for a websocket
            # one; a client of its own keeps the shared one usable for REST.
            api = CoreV1Api(api_client=ApiClient())
            kwargs: dict[str, Any] = dict(
                command=command, stderr=True, stdin=False, stdout=True, tty=False, _preload_content=False
            )
            if container:
                kwargs["container"] = container
            resp = stream(api.connect_get_namespaced_pod_exec, pod_name, ns, **kwargs)
            out: list[str] = []
            err: list[str] = []
            end = time.monotonic() + timeout
            try:
                while resp.is_open() and time.monotonic() < end:
                    resp.update(timeout=1)
                    if resp.peek_stdout():
                        out.append(resp.read_stdout())
                    if resp.peek_stderr():
                        err.append(resp.read_stderr())
                if resp.is_open():
                    raise TimeoutError(f"exec in '{pod_name}' did not finish within {timeout:.0f}s")
                code = resp.returncode
            finally:
                resp.close()
            return code, "".join(out), "".join(err)

        try:
            return await asyncio.to_thread(_run)
        except TimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None)
            text = str(exc)
            if status == 403 or "Forbidden" in text:
                raise KubernetesUnavailableError(
                    f"exec into '{pod_name}' is forbidden: ARGUS's ServiceAccount needs 'pods/exec' create "
                    "in this namespace (argus-helm-chart argusMcp.rbac.iocExec)."
                ) from exc
            if status == 404:
                raise KubernetesResourceNotFoundError(f"Pod '{pod_name}' not found in '{ns}'.") from exc
            kubelet = re.search(r"dialing ([\d.]+):10250, code (\d+)", text)
            if kubelet or ":10250/" in text:
                where = f" ({kubelet.group(1)}, HTTP {kubelet.group(2)})" if kubelet else ""
                raise KubernetesUnavailableError(
                    f"exec into '{pod_name}' failed: the API server cannot reach the kubelet on the pod's node{where}. "
                    "That is a node problem, not the IOC's; the pod itself may be fine."
                ) from exc
            raise KubernetesUnavailableError(f"exec into '{pod_name}' failed: {text[:300]}") from exc

    def _apps_v1(self) -> Any:
        from kubernetes.client import AppsV1Api

        return AppsV1Api()

    async def list_ioc_workloads(self, namespace: str | None = None) -> list[IocWorkload]:
        """Every ioc-chart StatefulSet and Deployment in the namespace (needs apps get/list)."""
        self._require_configured()
        ns = _check_name("namespace", self._namespace_or_default(namespace))
        apps = self._apps_v1()
        try:
            sts, deps = await asyncio.gather(
                asyncio.to_thread(apps.list_namespaced_stateful_set, ns, label_selector=IOC_CHART_SELECTOR),
                asyncio.to_thread(apps.list_namespaced_deployment, ns, label_selector=IOC_CHART_SELECTOR),
            )
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 403:
                raise KubernetesUnavailableError(
                    "Listing StatefulSets/Deployments is forbidden: ARGUS's ServiceAccount needs apps get/list "
                    "(argus-helm-chart argusMcp.rbac.leftoverDetection)."
                ) from exc
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        out = [_to_workload("StatefulSet", w) for w in sts.items] + [_to_workload("Deployment", w) for w in deps.items]
        out.sort(key=lambda w: (w.instance, w.kind))
        return out

    async def get_ioc_workload(self, kind: str, name: str, namespace: str) -> IocWorkload | None:
        self._require_configured()
        ns = _check_name("namespace", namespace)
        _check_name("workload", name)
        reader = {
            "StatefulSet": self._apps_v1().read_namespaced_stateful_set,
            "Deployment": self._apps_v1().read_namespaced_deployment,
        }.get(kind)
        if reader is None:
            raise KubernetesResourceNotFoundError(f"Unsupported workload kind {kind!r}.")
        try:
            return _to_workload(kind, await asyncio.to_thread(reader, name, ns))
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                return None
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc

    async def list_ioc_configs(self, namespace: str | None = None) -> dict[str, str]:
        """IOC name -> the config.yaml it was deployed with (ioc-chart ConfigMaps, label is_ioc=True)."""
        self._require_configured()
        ns = _check_name("namespace", self._namespace_or_default(namespace))
        try:
            result = await asyncio.to_thread(self._core_v1().list_namespaced_config_map, ns, label_selector="is_ioc=True")
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 403:
                raise KubernetesUnavailableError(
                    "Listing ConfigMaps is forbidden: ARGUS's ServiceAccount needs configmaps get/list "
                    "(argus-helm-chart argusMcp.rbac.leftoverDetection)."
                ) from exc
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        return {cm.metadata.name: (cm.data or {}).get("config.yaml", "") for cm in result.items}

    async def list_argocd_application_names(self, argocd_namespace: str) -> set[str]:
        """Names of the ArgoCD Applications that exist (needs applications get/list in ArgoCD's namespace)."""
        self._require_configured()
        ns = _check_name("namespace", argocd_namespace)
        from kubernetes.client import CustomObjectsApi

        try:
            result = await asyncio.to_thread(
                CustomObjectsApi().list_namespaced_custom_object, "argoproj.io", "v1alpha1", ns, "applications"
            )
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 403:
                raise KubernetesUnavailableError(
                    f"Reading ArgoCD Applications in '{ns}' is forbidden: ARGUS's ServiceAccount needs "
                    "applications.argoproj.io get/list there (argus-helm-chart argusMcp.rbac.leftoverDetection)."
                ) from exc
            raise KubernetesUnavailableError(f"Kubernetes API unreachable: {exc}") from exc
        return {item["metadata"]["name"] for item in result.get("items", [])}

    async def scale_ioc_workload(self, kind: str, name: str, namespace: str, replicas: int) -> None:
        self._require_configured()
        ns = _check_name("namespace", namespace)
        _check_name("workload", name)
        patcher = {
            "StatefulSet": self._apps_v1().patch_namespaced_stateful_set_scale,
            "Deployment": self._apps_v1().patch_namespaced_deployment_scale,
        }[kind]
        try:
            await asyncio.to_thread(patcher, name, ns, {"spec": {"replicas": replicas}})
        except Exception as exc:  # noqa: BLE001
            raise KubernetesUnavailableError(f"Could not scale {kind} '{name}': {exc}") from exc

    async def delete_ioc_workload(self, kind: str, name: str, namespace: str) -> None:
        self._require_configured()
        ns = _check_name("namespace", namespace)
        _check_name("workload", name)
        deleter = {
            "StatefulSet": self._apps_v1().delete_namespaced_stateful_set,
            "Deployment": self._apps_v1().delete_namespaced_deployment,
        }[kind]
        try:
            # Foreground: the call returns once Kubernetes has accepted it; pods go with it.
            await asyncio.to_thread(deleter, name, ns, propagation_policy="Foreground")
        except Exception as exc:  # noqa: BLE001
            raise KubernetesUnavailableError(f"Could not delete {kind} '{name}': {exc}") from exc

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


def _to_workload(kind: str, obj: Any) -> IocWorkload:
    meta, spec, status = obj.metadata, obj.spec, obj.status
    labels = meta.labels or {}
    tracking = (meta.annotations or {}).get(ARGOCD_TRACKING_ANNOTATION)
    created = meta.creation_timestamp
    if isinstance(created, datetime) and created.tzinfo is not None:
        created = created.replace(tzinfo=None)
    return IocWorkload(
        kind=kind,
        name=meta.name,
        namespace=meta.namespace,
        instance=labels.get("app.kubernetes.io/instance", meta.name),
        replicas=int(spec.replicas or 0) if spec else 0,
        ready_replicas=int(getattr(status, "ready_replicas", 0) or 0) if status else 0,
        created=created,
        argocd_app=tracking.split(":", 1)[0] if tracking else None,
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
