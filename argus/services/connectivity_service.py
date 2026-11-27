"""Connectivity check for the network devices an IOC controls.

"Is the device even reachable?" is the first question when an IOC misbehaves,
and the answer depends on *where* you ask from: many devices sit on private
networks (a power supplies' 192.168.0.x LAN, a camera VLAN) that only the IOC's
own pod can reach, through a Multus attachment or its node's routes. So the
check runs from up to two vantage points and compares them:

- ``argus``: from ARGUS's own pod (the cluster's routed network).
- ``ioc``: inside the IOC's pod, through ``pods/exec``, so the probe sees the
  IOC's exact network: its extra interfaces, routes and firewall position.

Targets come from the beamline's deploy values (the same YAML
list_beamline_devices reads): an IOC's ``host``/``server``/``ip``/``address``
with its ``port``, and each device's own address/port; GigE camera IDs get a
GigE Vision control-register read as well. Explicit hosts can be given too,
limited to private addresses so the tool can't be used to probe the internet.

Read-only: ping, TCP connect-and-close, one GVCP register read
(services/connectivity_probe.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any

import yaml

from argus.core.errors import ArgusError, ValidationError
from argus.providers.kubernetes.exceptions import KubernetesResourceNotFoundError
from argus.providers.kubernetes.kubernetes import KubernetesProvider
from argus.services import connectivity_probe
from argus.services.beamline_inventory_service import DEFAULT_INVENTORY_PATH, _merge_ioc_defaults
from argus.services.knowledge_service import KnowledgeService

_EXEC_TIMEOUT = 60.0
_ADDRESS_KEYS = ("host", "server", "ip", "address")
_ADDRESS_RE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$")
# Interpreters tried in the IOC pod, in order (exec does not go through a shell,
# so each is tried as an argv[0]). epik8s runtime images ship /venv/bin/python3.
_INTERPRETERS = ("python3", "/venv/bin/python3", "/usr/bin/python3", "python")
_PROBE_SOURCE = inspect.getsource(connectivity_probe)
EPIK8S_IOC_LABEL = "app.kubernetes.io/instance"
HIGH_RESTARTS = 20


class ConnectivityError(ArgusError):
    code = "connectivity_error"


@dataclass
class Target:
    host: str
    port: int | None = None
    kind: str | None = None  # "gige" for GigE Vision cameras
    source: str = "argument"  # where it came from, e.g. "ioc.host", "device SBN04 id"

    def wire(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "kind": self.kind}


@dataclass
class ConnectivityReport:
    subject: str
    findings: list[dict[str, str]] = field(default_factory=list)
    ioc_pod: dict[str, Any] | None = None
    targets: list[dict[str, Any]] = field(default_factory=list)
    vantages: dict[str, str] = field(default_factory=dict)  # vantage -> "ok" or why it didn't run
    notes: list[str] = field(default_factory=list)

    def finding(self, severity: str, message: str) -> None:
        self.findings.append({"severity": severity, "message": message})

    def to_dict(self) -> dict[str, Any]:
        order = {"critical": 0, "warning": 1, "info": 2}
        return {
            "subject": self.subject,
            "findings": sorted(self.findings, key=lambda f: order.get(f["severity"], 3)),
            "ioc_pod": self.ioc_pod,
            "targets": self.targets,
            "vantages": self.vantages,
            "notes": self.notes,
        }


# Config values that match the host pattern but are files (sparcmag.csv, st.cmd...).
_FILE_SUFFIXES = {
    "csv", "txt", "json", "yaml", "yml", "db", "cmd", "xml", "bob", "opi", "py", "sh", "template",
    "substitutions", "req", "proto", "dbd", "ini", "cfg", "conf", "log",
}


def _looks_like_address(value: Any) -> bool:
    if not isinstance(value, str) or not _ADDRESS_RE.match(value.strip()):
        return False
    last = value.strip().rsplit(".", 1)[-1].lower()
    return last.isdigit() or last not in _FILE_SUFFIXES


def _port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 < value < 65536:
        return value
    if isinstance(value, str) and value.isdigit() and 0 < int(value) < 65536:
        return int(value)
    return None


def targets_from_ioc_config(ioc: dict[str, Any]) -> list[Target]:
    """Every device address an (already defaults-merged) IOC entry declares."""
    targets: list[Target] = []
    seen: set[tuple[str, int | None]] = set()

    def add(host: str, port: int | None, kind: str | None, source: str) -> None:
        host = host.strip()
        if (host, port) in seen:
            return
        seen.add((host, port))
        targets.append(Target(host=host, port=port, kind=kind, source=source))

    is_camera = (ioc.get("template") or ioc.get("devtype")) in ("adcamera", "camera") or ioc.get("devgroup") == "cam"
    ioc_port = _port(ioc.get("port"))
    ioc_hosts = [(k, ioc[k]) for k in _ADDRESS_KEYS if _looks_like_address(ioc.get(k))]
    for key, host in ioc_hosts:
        add(host, ioc_port, None, f"ioc.{key}")

    for param in ioc.get("iocparam") or []:
        if isinstance(param, dict) and _looks_like_address(param.get("value")):
            add(param["value"], None, None, f"iocparam {param.get('name', '?')}")

    for dev in ioc.get("devices") or []:
        if not isinstance(dev, dict):
            continue
        name = dev.get("alias") or dev.get("name") or "?"
        dev_port = _port(dev.get("port"))
        dev_hosts = [(k, dev[k]) for k in _ADDRESS_KEYS if _looks_like_address(dev.get(k))]
        if is_camera and _looks_like_address(dev.get("id")):
            dev_hosts.append(("id", dev["id"]))
        for key, host in dev_hosts:
            kind = "gige" if is_camera and key == "id" else None
            add(host, dev_port, kind, f"device {name} {key}")
        if not dev_hosts and dev_port is not None:
            # The device is a port on the IOC's server (a serial-to-Ethernet
            # converter channel, a Modbus unit...): probe that server and port.
            for key, host in ioc_hosts:
                add(host, dev_port, None, f"device {name} port on ioc.{key}")
    return targets


def _check_explicit_host(host: str) -> None:
    if not _looks_like_address(host):
        raise ValidationError(f"Not a host name or IPv4 address: {host!r}")
    try:
        ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (socket.gaierror, IndexError):
        # Unresolvable here may still resolve in the IOC's pod; the probe reports it.
        return
    if not ipaddress.ip_address(ip).is_private:
        raise ValidationError(f"{host} ({ip}) is not a private address: only facility networks may be probed.")


def _reachable(result: dict[str, Any] | None) -> bool | None:
    """True/False from a probe result; None when the vantage didn't run or the host didn't resolve."""
    if result is None or result.get("ip") is None:
        return None
    ping = result.get("ping") or {}
    tcp = result.get("tcp") or {}
    gvcp = result.get("gvcp") or {}
    return (
        bool(ping.get("received"))
        or tcp.get("state") in ("open", "refused")
        or bool(tcp.get("held_by_this_pod"))
        or bool(gvcp.get("answered"))
    )


def _describe(target: Target) -> str:
    port = f":{target.port}" if target.port else ""
    return f"{target.host}{port} ({target.source})"


class ConnectivityService:
    def __init__(
        self,
        *,
        kubernetes: KubernetesProvider,
        knowledge: KnowledgeService,
        default_namespace: str,
        inventory_path: str = DEFAULT_INVENTORY_PATH,
    ) -> None:
        self._kubernetes = kubernetes
        self._knowledge = knowledge
        self._default_namespace = default_namespace
        self._inventory_path = inventory_path

    async def _ioc_config(self, ioc_name: str) -> dict[str, Any]:
        file = await self._knowledge.get_file(None, self._inventory_path, "HEAD")
        try:
            data = yaml.safe_load(file.content) or {}
        except yaml.YAMLError as exc:
            raise ConnectivityError(f"Could not parse {self._inventory_path}: {exc}") from exc
        iocs = (data.get("epicsConfiguration") or {}).get("iocs") or []
        if isinstance(iocs, dict):
            iocs = list(iocs.values())
        for raw in iocs:
            if isinstance(raw, dict) and raw.get("name") == ioc_name:
                return _merge_ioc_defaults(data.get("iocDefaults") or {}, raw)
        raise ConnectivityError(f"IOC {ioc_name!r} is not in {self._inventory_path}.")

    async def _targets_for_ioc(self, ioc: str, namespace: str, report: ConnectivityReport) -> list[Target]:
        """The IOC's device addresses: from the beamline config in git, else from its running pod.

        The pod fallback reads the config the IOC was actually deployed with
        (ioc-chart mounts it at /etc/config-dump/config.yaml, defaults already
        merged). It also catches IOCs that run but are no longer in git.
        """
        git_error: str | None = None
        try:
            return targets_from_ioc_config(await self._ioc_config(ioc))
        except ArgusError as exc:
            git_error = str(exc)
        pod = await self._find_pod(ioc, namespace)
        if pod is None:
            raise ConnectivityError(
                f"Could not find {ioc}'s device addresses: {git_error}, and no running pod to read its deployed "
                "config from. Pass hosts explicitly."
            )
        deployed = await self._deployed_config(pod, namespace)
        if "is not in" in (git_error or ""):
            report.finding(
                "warning",
                f"IOC {ioc} runs (pod {pod}) but is not in the beamline config: a leftover deployment outside git. "
                "Its device addresses were read from the pod's deployed config.",
            )
        else:
            report.notes.append(f"Beamline config unavailable ({git_error}); device addresses read from pod {pod}.")
        return targets_from_ioc_config(deployed)

    async def _deployed_config(self, pod: str, namespace: str) -> dict[str, Any]:
        reader = "import sys;sys.stdout.write(open('/etc/config-dump/config.yaml').read())"
        last = ""
        for interpreter in _INTERPRETERS:
            code, out, err = await self._kubernetes.exec_in_pod(pod, namespace, [interpreter, "-c", reader], timeout=20)
            if code == 0:
                try:
                    data = yaml.safe_load(out) or {}
                except yaml.YAMLError as exc:
                    raise ConnectivityError(f"{pod}'s deployed config is not YAML: {exc}") from exc
                if isinstance(data, dict):
                    return data
                raise ConnectivityError(f"{pod}'s deployed config is not a mapping.")
            last = (err or out or f"exit code {code}").strip()
            if not re.search(r"not found|no such file|executable file", last, re.I) or "config-dump" in last:
                break
        raise ConnectivityError(f"Could not read {pod}'s deployed config: {last[:200]}")

    async def _find_pod(self, ioc_name: str, namespace: str) -> str | None:
        pod = await self._kubernetes.get_pod_for_ioc(ioc_name, namespace)
        # Kubernetes names are lower case: an IOC configured as haz-tcp-SBN401
        # runs as haz-tcp-sbn401. Then the epik8s ioc-chart label as a fallback.
        for name in dict.fromkeys([ioc_name, ioc_name.lower()]):
            if pod is not None:
                break
            if name != ioc_name:
                pod = await self._kubernetes.get_pod_for_ioc(name, namespace)
            if pod is None:
                pods = await self._kubernetes.list_iocs(namespace, label_selector=f"{EPIK8S_IOC_LABEL}={name}")
                pod = pods[0] if pods else None
        return pod.name if pod else None

    async def _probe_in_pod(
        self, pod: str, namespace: str, container: str | None, targets: list[Target], count: int
    ) -> dict[str, Any]:
        request = json.dumps({"targets": [t.wire() for t in targets], "count": count, "timeout": 1.0})
        last_error = ""
        for interpreter in _INTERPRETERS:
            code, out, err = await self._kubernetes.exec_in_pod(
                pod, namespace, [interpreter, "-c", _PROBE_SOURCE, request], container=container, timeout=_EXEC_TIMEOUT
            )
            if code == 0 and out.strip():
                try:
                    return json.loads(out.strip().splitlines()[-1])
                except ValueError as exc:
                    raise ConnectivityError(f"Probe output from {pod} was not JSON: {out[:200]!r}") from exc
            last_error = (err or out or f"exit code {code}").strip()
            # Only a missing interpreter is worth retrying with the next one.
            if not re.search(r"not found|no such file|executable file", last_error, re.I):
                break
        raise ConnectivityError(f"The probe could not run in {pod}: {last_error[:300]}")

    async def check(
        self,
        *,
        ioc: str | None = None,
        hosts: list[str] | None = None,
        port: int | None = None,
        namespace: str | None = None,
        vantage: str = "auto",
        count: int = 3,
    ) -> ConnectivityReport:
        if vantage not in ("auto", "argus", "ioc", "both"):
            raise ValidationError("vantage must be one of auto, argus, ioc, both.")
        if not ioc and not hosts:
            raise ValidationError("Give an ioc, hosts, or both.")
        if vantage == "ioc" and not ioc:
            raise ValidationError("vantage 'ioc' needs the ioc whose pod to probe from.")
        ns = namespace or self._default_namespace
        report = ConnectivityReport(subject=ioc or ", ".join(hosts or []))

        targets: list[Target] = []
        if hosts:
            for host in hosts:
                _check_explicit_host(str(host))
                targets.append(Target(host=str(host).strip(), port=port, source="argument"))
        elif ioc:
            targets = await self._targets_for_ioc(ioc, ns, report)
            if port is not None:
                targets = [dataclasses.replace(t, port=port) for t in targets]
        if not targets:
            report.finding("info", f"{ioc} declares no device address (host/server/ip) in its config: nothing to probe.")
            return report
        targets = targets[: connectivity_probe.MAX_TARGETS]
        count = max(1, min(connectivity_probe.MAX_COUNT, int(count)))

        run_argus = vantage in ("argus", "both") or (vantage == "auto")
        run_ioc = bool(ioc) and vantage in ("ioc", "both", "auto")

        # The IOC's pod: where it runs, its networks, its health.
        pod_name = container = None
        high_restarts: tuple[str, int, str | None] | None = None
        if run_ioc:
            try:
                pod_name = await self._find_pod(ioc, ns)  # type: ignore[arg-type]
                if pod_name is None:
                    report.vantages["ioc"] = f"skipped: no pod found for IOC {ioc} in {ns}"
                else:
                    detail = await self._kubernetes.get_pod(pod_name, ns)
                    container = detail.containers[0].name if detail.containers else None
                    restarts = sum(c.restart_count for c in detail.containers)
                    report.ioc_pod = {
                        "pod": detail.name,
                        "node": detail.node,
                        "phase": detail.phase,
                        "ready": detail.ready,
                        "restarts": restarts,
                        "networks": [
                            {"name": n.name, "interface": n.interface, "ips": n.ips} for n in detail.networks
                        ],
                    }
                    if detail.phase != "Running":
                        report.vantages["ioc"] = f"skipped: pod {detail.name} is {detail.phase}, exec needs a running pod"
                        pod_name = None
                    last_end = next((c.last_termination for c in detail.containers if c.last_termination), None)
                    report.ioc_pod["last_termination"] = last_end
                    if restarts >= HIGH_RESTARTS:
                        high_restarts = (detail.name, restarts, last_end)
            except ArgusError as exc:
                report.vantages["ioc"] = f"unavailable: {exc}"
                pod_name = None

        async def argus_side() -> dict[str, Any]:
            return await asyncio.to_thread(connectivity_probe.probe, [t.wire() for t in targets], count, 1.0)

        coros: dict[str, Any] = {}
        if run_argus:
            coros["argus"] = argus_side()
        if pod_name:
            coros["ioc"] = self._probe_in_pod(pod_name, ns, container, targets, count)
        outcomes = await asyncio.gather(*coros.values(), return_exceptions=True)
        results: dict[str, list[dict[str, Any]]] = {}
        for name, outcome in zip(coros.keys(), outcomes, strict=True):
            if isinstance(outcome, BaseException):
                report.vantages[name] = f"unavailable: {outcome}"
            else:
                report.vantages[name] = "ok"
                results[name] = outcome.get("results", [])
        if run_argus and "argus" not in report.vantages:
            report.vantages["argus"] = "skipped"

        for i, target in enumerate(targets):
            from_argus = results.get("argus", [None] * len(targets))[i] if "argus" in results else None
            from_ioc = results.get("ioc", [None] * len(targets))[i] if "ioc" in results else None
            entry: dict[str, Any] = {"target": target.host, "port": target.port, "source": target.source}
            if target.kind:
                entry["kind"] = target.kind
            if from_ioc is not None:
                entry["from_ioc"] = from_ioc
            if from_argus is not None:
                entry["from_argus"] = from_argus
            entry["verdict"] = self._verdict(report, target, from_argus, from_ioc, ioc_ran="ioc" in results)
            report.targets.append(entry)

        if report.targets:
            report.finding("info", _summary(report.targets))

        if high_restarts:
            name, restarts, last_end = high_restarts
            last = f" The last restart: {last_end}." if last_end else ""
            if any(t["verdict"] in ("unreachable", "unreachable_from_ioc", "dns_failure", "host_up_port_closed")
                   for t in report.targets):
                report.finding(
                    "critical",
                    f"IOC pod {name} has restarted {restarts} times and some of its devices are unreachable "
                    f"(below): the IOC most likely fails because it cannot talk to them.{last}",
                )
            else:
                report.finding(
                    "warning",
                    f"IOC pod {name} has restarted {restarts} times, but its devices answer now: the restarts "
                    f"happened earlier (e.g. while a device was off) or have another cause - check the IOC "
                    f"log.{last}",
                )

        if "ioc" in report.vantages and report.vantages["ioc"] != "ok" and ioc:
            report.notes.append(
                "The IOC-side probe did not run, so results are from ARGUS's network only. Devices on private "
                "networks (power-supply LANs, camera VLANs) are often reachable only from the IOC's pod: an "
                "'unreachable from ARGUS' there is not conclusive."
            )
        return report

    def _verdict(
        self,
        report: ConnectivityReport,
        target: Target,
        from_argus: dict[str, Any] | None,
        from_ioc: dict[str, Any] | None,
        *,
        ioc_ran: bool,
    ) -> str:
        what = _describe(target)
        primary = from_ioc if ioc_ran else from_argus
        if primary is not None and primary.get("ip") is None:
            where = "the IOC's pod" if ioc_ran else "ARGUS"
            report.finding("critical", f"{what}: name does not resolve from {where} ({primary.get('error')}).")
            return "dns_failure"

        ioc_ok = _reachable(from_ioc)
        argus_ok = _reachable(from_argus)

        if ioc_ok is True or (not ioc_ran and argus_ok is True):
            result = from_ioc if ioc_ok else from_argus
            assert result is not None
            tcp = result.get("tcp") or {}
            gvcp = result.get("gvcp") or {}
            if target.port and tcp.get("state") != "open" and tcp.get("held_by_this_pod") and result is from_ioc:
                # One-client servers (Moxa & co.) refuse a second connection while the IOC holds the port.
                return "in_use_by_ioc"
            if target.port and tcp.get("state") != "open":
                report.finding(
                    "warning",
                    f"{what}: the host answers, but TCP port {target.port} is {tcp.get('state')} - the device's "
                    "service is down, or the port is held/blocked.",
                )
                return "host_up_port_closed"
            if target.kind == "gige" and gvcp.get("answered") and not gvcp.get("controlled"):
                report.finding(
                    "warning",
                    f"{what}: the camera answers but no application controls it (CCP {gvcp.get('ccp')}): "
                    "the IOC is not connected to it.",
                )
                return "camera_not_controlled"
            if ioc_ran and argus_ok is False:
                return "reachable_from_ioc_only"  # normal for private device networks
            return "reachable"

        if ioc_ok is False and argus_ok is True:
            report.finding(
                "critical",
                f"{what}: reachable from ARGUS but NOT from the IOC's pod - the IOC's own network path "
                "(its VLAN attachment, routes or node) is the problem, not the device.",
            )
            return "unreachable_from_ioc"
        if ioc_ok is False:
            report.finding(
                "critical",
                f"{what}: no answer from the IOC's pod (nor from ARGUS) - the device is off, unplugged, or its "
                "network is down.",
            )
            return "unreachable"
        if argus_ok is False:
            if ioc_ran:
                return "unreachable"
            report.finding(
                "warning" if target.host and _is_private_name(from_argus) else "critical",
                f"{what}: no answer from ARGUS's network. Not conclusive for devices on a private network the IOC "
                "reaches directly: check from the IOC (vantage 'ioc').",
            )
            return "unreachable_from_argus"
        return "not_tested"


_VERDICT_WORDS = {
    "reachable": "reachable",
    "reachable_from_ioc_only": "reachable from the IOC only (normal on a private device network)",
    "in_use_by_ioc": "connected to the IOC right now (port held by the IOC: healthy)",
    "host_up_port_closed": "host up but port closed",
    "camera_not_controlled": "camera not controlled by the IOC",
    "unreachable": "unreachable",
    "unreachable_from_ioc": "unreachable from the IOC",
    "unreachable_from_argus": "unreachable from ARGUS",
    "dns_failure": "DNS failure",
    "not_tested": "not tested",
}
_HEALTHY = {"reachable", "reachable_from_ioc_only", "in_use_by_ioc"}


def _summary(targets: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for t in targets:
        counts[t["verdict"]] = counts.get(t["verdict"], 0) + 1
    total = len(targets)
    healthy = sum(n for v, n in counts.items() if v in _HEALTHY)
    parts = ", ".join(f"{n} {_VERDICT_WORDS.get(v, v)}" for v, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    if healthy == total:
        return f"All {total} devices checked are OK: {parts}."
    return f"{total} devices checked, {healthy} OK and {total - healthy} with problems: {parts}."


def _is_private_name(result: dict[str, Any] | None) -> bool:
    ip = (result or {}).get("ip")
    try:
        return bool(ip) and ipaddress.ip_address(ip).is_private
    except ValueError:
        return False
