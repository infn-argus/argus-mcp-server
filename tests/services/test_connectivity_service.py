from __future__ import annotations

import json

import pytest

from argus.core.errors import ValidationError
from argus.providers.kubernetes.exceptions import KubernetesUnavailableError
from argus.providers.kubernetes.models import ContainerDetail, PodDetail, PodNetworkAttachment, PodStatus
from argus.services import connectivity_probe, connectivity_service
from argus.services.connectivity_service import ConnectivityService, targets_from_ioc_config

VALUES = """
iocDefaults:
  adcamera:
    devgroup: cam
    devtype: camera
epicsConfiguration:
  iocs:
    ac2bpm01:
      name: ac2bpm01
      template: libera-sppp
      host: bdsparcac2bpm001.lnf.infn.it
    ocem-modbus:
      name: ocem-modbus
      devgroup: mag
      devices:
        - name: PS1
          ip: 192.168.0.152
          port: 502
        - name: PS2
          ip: 192.168.0.158
          port: 502
    vac-midivac:
      name: vac-midivac
      server: scsparcenea001.lnf.infn.it
      devices:
        - name: P1
          port: 4001
        - name: P2
          port: 4002
    sbncamera-04:
      name: sbncamera-04
      template: adcamera
      devices:
        - name: SBN04
          id: 192.168.110.62
    haz-ser:
      name: haz-ser
      server: scsparchazmxa002.lnf.infn.it
      port: 4001
      iocparam:
        - name: gateway
          value: plsparcrasb005.lnf.infn.it
        - name: speed
          value: "9600"
      devices:
        - name: CH1
          id: "6"
"""


def _merge(name):
    import yaml

    from argus.services.beamline_inventory_service import _merge_ioc_defaults

    data = yaml.safe_load(VALUES)
    return _merge_ioc_defaults(data["iocDefaults"], data["epicsConfiguration"]["iocs"][name])


def test_targets_ioc_host():
    [t] = targets_from_ioc_config(_merge("ac2bpm01"))
    assert (t.host, t.port, t.kind, t.source) == ("bdsparcac2bpm001.lnf.infn.it", None, None, "ioc.host")


def test_targets_device_ip_and_port():
    targets = targets_from_ioc_config(_merge("ocem-modbus"))
    assert [(t.host, t.port) for t in targets] == [("192.168.0.152", 502), ("192.168.0.158", 502)]


def test_targets_device_port_on_ioc_server():
    targets = targets_from_ioc_config(_merge("vac-midivac"))
    assert [(t.host, t.port) for t in targets] == [
        ("scsparcenea001.lnf.infn.it", None),
        ("scsparcenea001.lnf.infn.it", 4001),
        ("scsparcenea001.lnf.infn.it", 4002),
    ]


def test_targets_camera_id_is_gige():
    [t] = targets_from_ioc_config(_merge("sbncamera-04"))
    assert (t.host, t.kind) == ("192.168.110.62", "gige")


def test_targets_ignore_non_addresses_and_take_iocparam_hosts():
    targets = targets_from_ioc_config(_merge("haz-ser"))
    # The serial id "6" and the "9600" iocparam are not addresses.
    assert [(t.host, t.port) for t in targets] == [
        ("scsparchazmxa002.lnf.infn.it", 4001),
        ("plsparcrasb005.lnf.infn.it", None),
    ]


class FakeKnowledge:
    def __init__(self, content=VALUES, error=None):
        self.content, self.error = content, error

    async def get_file(self, repo, path, ref):
        if self.error:
            raise self.error

        class File:
            content = self.content

        return File()


def _pod_detail(name="ac2bpm01-0", restarts=0, phase="Running", last=None):
    return PodDetail(
        name=name, namespace="sparc", phase=phase, ready=phase == "Running", node="kube-chaos04",
        pod_ip="10.42.0.5", host_ip="192.168.108.20", host_network=False, started_at=None, owner=None,
        containers=[ContainerDetail(name="ioc", image="img", ready=True, restart_count=restarts, state="running",
                                    last_termination=last)],
        networks=[PodNetworkAttachment(name="sparc/sparc-magnets", interface="net1", ips=["192.168.0.201"])],
    )


class FakeKube:
    def __init__(self, *, pod="ac2bpm01-0", detail=None, ioc_results=None, exec_error=None, deployed=None):
        self.pod, self.detail = pod, detail or _pod_detail()
        self.ioc_results, self.exec_error, self.deployed = ioc_results, exec_error, deployed
        self.execs: list[list[str]] = []

    async def get_pod_for_ioc(self, ioc_name, namespace=None):
        return PodStatus(name=self.pod, namespace="sparc", phase="Running", ready=True, restart_count=0,
                         node=None, started_at=None) if self.pod else None

    async def list_iocs(self, namespace=None, *, label_selector=None):
        return []

    async def get_pod(self, name, namespace=None):
        return self.detail

    async def exec_in_pod(self, pod, namespace, command, *, container=None, timeout=30.0):
        self.execs.append(command)
        if self.exec_error:
            raise self.exec_error
        if "config-dump" in command[-1]:
            return 0, self.deployed or "", ""
        request = json.loads(command[-1])
        results = self.ioc_results(request["targets"]) if callable(self.ioc_results) else self.ioc_results
        return 0, json.dumps({"probe_version": 1, "hostname": pod, "results": results}), ""


def _res(host, *, received=0, ip="192.0.2.1", tcp=None, gvcp=None):
    r = {"host": host, "ip": ip, "ping": {"method": "ping-socket", "sent": 2, "received": received}}
    if tcp:
        r["tcp"] = tcp
    if gvcp:
        r["gvcp"] = gvcp
    return r


@pytest.fixture
def argus_probe(monkeypatch):
    """Replace ARGUS's own probe: the test sets what ARGUS's network sees."""
    state = {"fn": lambda targets: [_res(t["host"]) for t in targets]}

    def fake_probe(targets, count=3, timeout=1.0):
        return {"probe_version": 1, "hostname": "argus", "results": state["fn"](targets)}

    monkeypatch.setattr(connectivity_probe, "probe", fake_probe)
    monkeypatch.setattr(connectivity_service, "_check_explicit_host", lambda host: None)
    return state


def _service(kube, knowledge=None):
    return ConnectivityService(kubernetes=kube, knowledge=knowledge or FakeKnowledge(), default_namespace="sparc")


async def test_private_device_reachable_only_from_ioc(argus_probe):
    kube = FakeKube(ioc_results=lambda ts: [_res(t["host"], received=2) for t in ts])
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert report["vantages"] == {"argus": "ok", "ioc": "ok"}
    assert report["targets"][0]["verdict"] == "reachable_from_ioc_only"
    # All fine is said explicitly, so a model never has to infer it from an empty list.
    [summary] = report["findings"]
    assert summary["severity"] == "info"
    assert summary["message"].startswith("All 1 devices checked are OK: 1 reachable from the IOC only")
    # The probe ran in the IOC's pod with an argv (no shell), interpreter first.
    assert kube.execs[0][0] == "python3" and kube.execs[0][1] == "-c"


async def test_device_off_is_critical(argus_probe):
    kube = FakeKube(ioc_results=lambda ts: [_res(t["host"]) for t in ts])
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert report["targets"][0]["verdict"] == "unreachable"
    assert report["findings"][0]["severity"] == "critical"
    assert "off, unplugged" in report["findings"][0]["message"]


async def test_reachable_from_argus_not_from_ioc_blames_ioc_path(argus_probe):
    argus_probe["fn"] = lambda ts: [_res(t["host"], received=2) for t in ts]
    kube = FakeKube(ioc_results=lambda ts: [_res(t["host"]) for t in ts])
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert report["targets"][0]["verdict"] == "unreachable_from_ioc"
    assert "IOC's own network path" in report["findings"][0]["message"]


async def test_port_closed_but_host_up(argus_probe):
    kube = FakeKube(
        ioc_results=lambda ts: [_res(t["host"], received=2, tcp={"port": t["port"], "state": "refused"}) for t in ts]
    )
    report = (await _service(kube).check(ioc="ocem-modbus")).to_dict()
    assert {t["verdict"] for t in report["targets"]} == {"host_up_port_closed"}
    assert "TCP port 502 is refused" in report["findings"][0]["message"]


async def test_port_refused_but_held_by_the_ioc_is_healthy(argus_probe):
    # One-client serial servers (Moxa) refuse a second connection while the IOC holds the port.
    kube = FakeKube(
        ioc_results=lambda ts: [
            _res(t["host"], received=2, tcp={"port": t["port"], "state": "refused", "held_by_this_pod": True})
            for t in ts
        ]
    )
    report = (await _service(kube).check(ioc="ocem-modbus")).to_dict()
    assert {t["verdict"] for t in report["targets"]} == {"in_use_by_ioc"}
    [summary] = report["findings"]
    assert summary["severity"] == "info" and "All 2 devices checked are OK" in summary["message"]
    assert "held by the IOC" in summary["message"]


async def test_held_by_ioc_counts_as_reachable_without_ping(argus_probe):
    # ICMP filtered, but the IOC's live connection proves the device is there.
    kube = FakeKube(
        ioc_results=lambda ts: [
            _res(t["host"], tcp={"port": t["port"], "state": "timeout", "held_by_this_pod": True}) for t in ts
        ]
    )
    report = (await _service(kube).check(ioc="ocem-modbus")).to_dict()
    assert {t["verdict"] for t in report["targets"]} == {"in_use_by_ioc"}


async def test_summary_counts_problems(argus_probe):
    kube = FakeKube(
        ioc_results=lambda ts: [
            _res(ts[0]["host"], received=2, tcp={"port": 502, "state": "open"}),
            _res(ts[1]["host"]),
        ]
    )
    report = (await _service(kube).check(ioc="ocem-modbus")).to_dict()
    summary = [f for f in report["findings"] if f["severity"] == "info"][0]["message"]
    assert summary == "2 devices checked, 1 OK and 1 with problems: 1 reachable from the IOC only (normal on a private device network), 1 unreachable."


async def test_camera_not_controlled(argus_probe):
    kube = FakeKube(
        ioc_results=lambda ts: [_res(t["host"], received=2, gvcp={"answered": True, "ccp": "0x0", "controlled": False})
                                for t in ts]
    )
    report = (await _service(kube).check(ioc="sbncamera-04")).to_dict()
    assert report["targets"][0]["kind"] == "gige"
    assert report["targets"][0]["verdict"] == "camera_not_controlled"


async def test_dns_failure(argus_probe):
    kube = FakeKube(ioc_results=lambda ts: [{"host": t["host"], "ip": None, "error": "DNS lookup failed"} for t in ts])
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert report["targets"][0]["verdict"] == "dns_failure"
    assert report["findings"][0]["severity"] == "critical"


async def test_exec_forbidden_falls_back_to_argus_with_note(argus_probe):
    kube = FakeKube(exec_error=KubernetesUnavailableError("exec into 'ac2bpm01-0' is forbidden"))
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert report["vantages"]["ioc"].startswith("unavailable") and report["vantages"]["argus"] == "ok"
    assert report["targets"][0]["verdict"] == "unreachable_from_argus"
    assert any("not conclusive" in n for n in report["notes"])


async def test_high_restarts_with_devices_up_says_so(argus_probe):
    kube = FakeKube(
        detail=_pod_detail(restarts=7728, last="Completed (exit 0) at 2026-11-25T12:58:50"),
        ioc_results=lambda ts: [_res(t["host"], received=2) for t in ts],
    )
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    [finding] = [f for f in report["findings"] if f["severity"] != "info"]
    assert finding["severity"] == "warning" and "devices answer now" in finding["message"]
    assert "2026-11-25" in finding["message"]


async def test_high_restarts_with_device_down_is_the_cause(argus_probe):
    kube = FakeKube(detail=_pod_detail(restarts=50), ioc_results=lambda ts: [_res(t["host"]) for t in ts])
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert any(f["severity"] == "critical" and "cannot talk to them" in f["message"] for f in report["findings"])


async def test_ioc_not_in_git_reads_deployed_config(argus_probe):
    kube = FakeKube(
        pod="cplxbpm02-0",
        deployed="name: cplxbpm02\nhost: bdsparcplxbpm002.lnf.infn.it\n",
        ioc_results=lambda ts: [_res(t["host"], received=2) for t in ts],
    )
    report = (await _service(kube).check(ioc="cplxbpm02")).to_dict()
    assert report["targets"][0]["target"] == "bdsparcplxbpm002.lnf.infn.it"
    assert any("not in the beamline config" in f["message"] for f in report["findings"])


async def test_pod_not_running_skips_ioc_vantage(argus_probe):
    kube = FakeKube(detail=_pod_detail(phase="Pending"))
    report = (await _service(kube).check(ioc="ac2bpm01")).to_dict()
    assert report["vantages"]["ioc"].startswith("skipped")
    assert kube.execs == []


async def test_argument_validation():
    service = _service(FakeKube())
    with pytest.raises(ValidationError):
        await service.check()
    with pytest.raises(ValidationError):
        await service.check(hosts=["192.168.0.5"], vantage="ioc")
    with pytest.raises(ValidationError):
        await service.check(ioc="ac2bpm01", vantage="router")


def test_explicit_public_host_refused():
    with pytest.raises(ValidationError):
        connectivity_service._check_explicit_host("8.8.8.8")
    with pytest.raises(ValidationError):
        connectivity_service._check_explicit_host("bad host; rm -rf /")
    connectivity_service._check_explicit_host("192.168.0.5")


def test_probe_handles_bad_and_unresolvable_hosts():
    out = connectivity_probe.probe([{"host": "bad host"}, {"host": "does-not-exist.invalid"}], count=1, timeout=0.2)
    assert out["results"][0]["error"] == "invalid host"
    assert out["results"][1]["ip"] is None and "DNS" in out["results"][1]["error"]


def test_probe_tcp_refused_on_localhost():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Nothing listens on the port any more: connect is refused.
    out = connectivity_probe.probe([{"host": "127.0.0.1", "port": port}], count=1, timeout=0.5)
    assert out["results"][0]["tcp"]["state"] == "refused"


def test_probe_sees_a_connection_this_process_holds():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)):
            conn, _ = server.accept()
            with conn:
                held = connectivity_probe._held_connection("127.0.0.1", port)
    import sys

    assert held is (sys.platform.startswith("linux"))  # /proc/net/tcp is Linux-only
    assert connectivity_probe._held_connection("127.0.0.1", 1) is False


def test_hex_endpoint_matches_kernel_format():
    # 192.168.104.103:4001 as /proc/net/tcp prints it (seen in the haz-ser-ch1 pod).
    assert "6768A8C0:0FA1" in connectivity_probe._hex_endpoint("192.168.104.103", 4001)


def test_probe_source_runs_standalone(tmp_path):
    """The file is sent to IOC pods as `python3 -c <source>`: it must run on its own."""
    import subprocess
    import sys

    request = json.dumps({"targets": [{"host": "does-not-exist.invalid"}], "count": 1, "timeout": 0.2})
    out = subprocess.run(
        [sys.executable, "-I", "-c", connectivity_service._PROBE_SOURCE, request],
        capture_output=True, text=True, timeout=30, cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["results"][0]["ip"] is None
