"""Leftover IOC deployments: IOCs still running that nothing manages any more.

A renamed or removed IOC in a beamline's config should take its StatefulSet
with it, but it doesn't always: the ArgoCD Application is deleted without
cascading, or someone deployed by hand. The old pods keep running - and an IOC
that holds a device (a GigE camera, a serial port, a power supply's single TCP
session) stops the new IOC from getting control of it. That is what kept
SBN02-04 from streaming in November 2026.

A workload rendered by ioc-chart is a leftover when:
- its ArgoCD tracking annotation names an Application that no longer exists
  ("orphan"), or
- it has no tracking annotation and its IOC is not in the beamline config
  ("unmanaged": deployed by hand).
When ArgoCD's Applications can't be read, an IOC missing from the beamline
config is reported as "suspected", never removed.

For each leftover, its deployed config (the ioc-chart ConfigMap) is compared
with every managed IOC's: a shared device address means the two compete for
that device.

Removal is a separate, confirmed write: scale to 0 (reversible, the default)
or delete the StatefulSet/Deployment. It re-checks the classification at the
moment it runs and refuses anything that is managed or can't be verified.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

from argus.core.errors import ArgusError, ValidationError
from argus.providers.kubernetes.kubernetes import KubernetesProvider
from argus.providers.kubernetes.models import IocWorkload
from argus.services.beamline_inventory_service import DEFAULT_INVENTORY_PATH
from argus.services.connectivity_service import Target, targets_from_ioc_config
from argus.services.knowledge_service import KnowledgeService

LEFTOVER_STATES = ("orphan", "unmanaged", "suspected")


class LeftoverError(ArgusError):
    code = "leftover_error"


@dataclass
class Leftover:
    kind: str
    name: str
    instance: str
    state: str  # orphan | unmanaged | suspected
    reason: str
    replicas: int
    ready_replicas: int
    age_days: int | None
    argocd_app: str | None
    in_beamline_config: bool | None
    devices: list[str] = field(default_factory=list)
    # Managed IOCs it shares a device address with: [{"ioc": ..., "device": ...}]
    conflicts: list[dict[str, str]] = field(default_factory=list)
    removable: bool = False


@dataclass
class LeftoverReport:
    namespace: str
    findings: list[dict[str, str]] = field(default_factory=list)
    leftovers: list[Leftover] = field(default_factory=list)
    managed_iocs: int = 0
    sources: dict[str, str] = field(default_factory=dict)

    def finding(self, severity: str, message: str) -> None:
        self.findings.append({"severity": severity, "message": message})

    def to_dict(self) -> dict[str, Any]:
        order = {"critical": 0, "warning": 1, "info": 2}
        return {
            "namespace": self.namespace,
            "findings": sorted(self.findings, key=lambda f: order.get(f["severity"], 3)),
            "leftovers": [dataclasses.asdict(x) for x in self.leftovers],
            "managed_iocs": self.managed_iocs,
            "sources": self.sources,
        }


def _key(target: Target) -> tuple[str, int | None]:
    return target.host.strip().lower(), target.port


def _shares_device(a: Target, b: Target) -> bool:
    # Same host, and the same port or no port on either side (a serial-to-
    # Ethernet converter serves many IOCs on different ports: not a conflict).
    return a.host.strip().lower() == b.host.strip().lower() and a.port == b.port


def _config_targets(config_text: str) -> list[Target]:
    try:
        data = yaml.safe_load(config_text) or {}
    except yaml.YAMLError:
        return []
    return targets_from_ioc_config(data) if isinstance(data, dict) else []


def _device_claims(targets: list[Target]) -> list[Target]:
    """The addresses an IOC actually holds: on a host where it names ports (a serial-to-Ethernet
    converter or Raspberry with many ports), its port-less entry for that host is not a claim -
    other IOCs use the same host on their own ports."""
    hosts_with_ports = {t.host.strip().lower() for t in targets if t.port}
    return [t for t in targets if t.port or t.host.strip().lower() not in hosts_with_ports]


class LeftoverService:
    def __init__(
        self,
        *,
        kubernetes: KubernetesProvider,
        knowledge: KnowledgeService,
        default_namespace: str,
        argocd_namespace: str = "argocd",
        inventory_path: str = DEFAULT_INVENTORY_PATH,
    ) -> None:
        self._kubernetes = kubernetes
        self._knowledge = knowledge
        self._default_namespace = default_namespace
        self._argocd_namespace = argocd_namespace
        self._inventory_path = inventory_path

    async def _config_ioc_names(self) -> set[str] | None:
        try:
            file = await self._knowledge.get_file(None, self._inventory_path, "HEAD")
            data = yaml.safe_load(file.content) or {}
        except (ArgusError, yaml.YAMLError):
            return None
        iocs = (data.get("epicsConfiguration") or {}).get("iocs") or []
        names = [i.get("name") for i in (iocs.values() if isinstance(iocs, dict) else iocs) if isinstance(i, dict)]
        # softiocs are rendered by the same chart (epicsConfiguration.softiocs or top-level softiocs).
        soft = (data.get("epicsConfiguration") or {}).get("softiocs") or data.get("softiocs") or []
        names += [i.get("name") for i in (soft.values() if isinstance(soft, dict) else soft) if isinstance(i, dict)]
        return {str(n).lower() for n in names if n}

    def _classify(
        self, w: IocWorkload, apps: set[str] | None, config_names: set[str] | None
    ) -> tuple[str, str, bool | None]:
        """(state, reason, in_config). state is 'managed' or one of LEFTOVER_STATES."""
        in_config = None if config_names is None else w.instance.lower() in config_names
        if w.argocd_app:
            if apps is None:
                if in_config is False:
                    return "suspected", (
                        f"its ArgoCD app {w.argocd_app} could not be checked, and {w.instance} is not in the "
                        "beamline config"
                    ), in_config
                return "managed", "", in_config
            if w.argocd_app in apps:
                return "managed", "", in_config
            return "orphan", f"its ArgoCD app {w.argocd_app} no longer exists", in_config
        if in_config is False:
            return "unmanaged", "not deployed by ArgoCD and not in the beamline config (deployed by hand)", in_config
        if in_config is None:
            return "suspected", "not deployed by ArgoCD; the beamline config could not be read to check", in_config
        return "managed", "", in_config  # in the config, deployed by hand: unusual, but not a leftover

    @staticmethod
    def _removable(state: str, w: IocWorkload, apps: set[str] | None, in_config: bool | None) -> bool:
        if state == "orphan":
            return apps is not None
        if state == "unmanaged":
            return in_config is False
        return False

    async def _context(self, ns: str, report: LeftoverReport | None = None):
        apps: set[str] | None
        try:
            apps = await self._kubernetes.list_argocd_application_names(self._argocd_namespace)
            if report:
                report.sources["argocd_applications"] = "ok"
        except ArgusError as exc:
            apps = None
            if report:
                report.sources["argocd_applications"] = f"unavailable: {exc}"
        config_names = await self._config_ioc_names()
        if report:
            report.sources["beamline_config"] = "ok" if config_names is not None else "unavailable"
        return apps, config_names

    async def find(self, namespace: str | None = None) -> LeftoverReport:
        ns = namespace or self._default_namespace
        report = LeftoverReport(namespace=ns)
        workloads = await self._kubernetes.list_ioc_workloads(ns)
        report.sources["workloads"] = "ok"
        apps, config_names = await self._context(ns, report)
        try:
            configs = await self._kubernetes.list_ioc_configs(ns)
            report.sources["deployed_configs"] = "ok"
        except ArgusError as exc:
            configs = {}
            report.sources["deployed_configs"] = f"unavailable: {exc}"

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        managed_targets: list[tuple[str, Target]] = []
        candidates: list[tuple[IocWorkload, str, str, bool | None]] = []
        for w in workloads:
            state, reason, in_config = self._classify(w, apps, config_names)
            if state == "managed":
                report.managed_iocs += 1
                if w.replicas > 0:
                    managed_targets += [(w.instance, t) for t in _device_claims(_config_targets(configs.get(w.instance, "")))]
            else:
                candidates.append((w, state, reason, in_config))

        for w, state, reason, in_config in candidates:
            targets = _config_targets(configs.get(w.instance, ""))
            leftover = Leftover(
                kind=w.kind,
                name=w.name,
                instance=w.instance,
                state=state,
                reason=reason,
                replicas=w.replicas,
                ready_replicas=w.ready_replicas,
                age_days=(now - w.created).days if w.created else None,
                argocd_app=w.argocd_app,
                in_beamline_config=in_config,
                devices=[f"{t.host}:{t.port}" if t.port else t.host for t in targets],
                removable=self._removable(state, w, apps, in_config),
            )
            seen: set[tuple[str, str]] = set()
            for t in _device_claims(targets):
                for ioc, other in managed_targets:
                    if _shares_device(t, other) and (ioc, other.host) not in seen:
                        seen.add((ioc, other.host))
                        port = f":{other.port}" if other.port else ""
                        leftover.conflicts.append({"ioc": ioc, "device": f"{other.host}{port}"})
            report.leftovers.append(leftover)

            running = w.replicas > 0
            if leftover.conflicts and running:
                ours = ", ".join(f"{c['ioc']} ({c['device']})" for c in leftover.conflicts)
                report.finding(
                    "critical",
                    f"Leftover {w.kind} {w.name} ({reason}) is running and uses the same device as {ours}: the two "
                    "compete for it, so the current IOC may not get control. Remove the leftover "
                    "(remove_leftover_ioc).",
                )
            elif running:
                report.finding(
                    "warning",
                    f"Leftover {w.kind} {w.name} ({reason}) is still running ({w.ready_replicas}/{w.replicas} ready)"
                    + (f", {leftover.age_days} days old" if leftover.age_days is not None else "")
                    + ". It shares no device with a current IOC, but it uses resources and may publish stale PVs.",
                )
            else:
                report.finding("info", f"Leftover {w.kind} {w.name} ({reason}) is scaled to 0; it can be deleted.")
            if state == "suspected":
                report.findings[-1]["message"] += " (Suspected only: ArgoCD's applications could not be checked.)"
        if not report.leftovers:
            report.finding("info", f"No leftover IOC deployments in {ns}: every IOC workload is managed.")
        return report

    async def remove(
        self, name: str, *, namespace: str | None = None, kind: str = "StatefulSet", action: str = "scale_to_zero"
    ) -> dict[str, Any]:
        if action not in ("scale_to_zero", "delete"):
            raise ValidationError("action must be scale_to_zero or delete.")
        if kind not in ("StatefulSet", "Deployment"):
            raise ValidationError("kind must be StatefulSet or Deployment.")
        ns = namespace or self._default_namespace
        workload = await self._kubernetes.get_ioc_workload(kind, name, ns)
        if workload is None:
            raise LeftoverError(f"{kind} {name} does not exist in {ns} (or is not an ioc-chart workload).")
        apps, config_names = await self._context(ns)
        state, reason, in_config = self._classify(workload, apps, config_names)
        if state == "managed":
            raise LeftoverError(
                f"{kind} {name} is managed (ArgoCD app {workload.argocd_app or '-'}, in the beamline config: "
                f"{in_config}). Refusing to touch it: change the beamline config instead."
            )
        if not self._removable(state, workload, apps, in_config):
            raise LeftoverError(
                f"{kind} {name} looks like a leftover ({reason}) but that can't be confirmed now "
                "(ArgoCD's applications or the beamline config are unreadable). Refusing."
            )
        if action == "scale_to_zero":
            await self._kubernetes.scale_ioc_workload(kind, name, ns, 0)
            undo = f"kubectl -n {ns} scale {kind.lower()} {name} --replicas={max(1, workload.replicas)}"
        else:
            await self._kubernetes.delete_ioc_workload(kind, name, ns)
            undo = None
        return {
            "workload": f"{kind}/{name}",
            "namespace": ns,
            "action": action,
            "why_leftover": reason,
            "previous_replicas": workload.replicas,
            "undo": undo,
            "note": (
                "Only the workload was changed; its Service, ConfigMap and any Ingress stay (harmless, and they "
                "go if the workload is deleted later by hand)."
                if action == "delete"
                else "Scaled to 0: its pods stop and release their devices. Delete it once confirmed."
            ),
        }
