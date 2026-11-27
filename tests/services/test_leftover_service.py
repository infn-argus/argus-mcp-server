from __future__ import annotations

from datetime import datetime

import pytest

from argus.core.errors import ValidationError
from argus.providers.kubernetes.exceptions import KubernetesUnavailableError
from argus.providers.kubernetes.models import IocWorkload
from argus.services.leftover_service import LeftoverError, LeftoverService

VALUES = """
epicsConfiguration:
  iocs:
    sbncamera-02: {name: sbncamera-02}
    thor-rasb-las001: {name: thor-rasb-las001}
    haz-tcp-SBN401: {name: haz-tcp-SBN401}
"""

CONFIGS = {
    # current IOC and an old one on the same camera
    "sbncamera-02": "name: sbncamera-02\ntemplate: adcamera\ndevices:\n  - {name: SBN02, id: 192.168.110.41}\n",
    "sbn01": "name: sbn01\ntemplate: adcamera\ndevices:\n  - {name: SBN02, id: 192.168.110.41}\n",
    # same serial-to-Ethernet host, different ports: not a conflict
    "thor-rasb-las001": "name: thor-rasb-las001\nserver: plsparcrasb010.lnf.infn.it\nport: 4001\n"
    "iocparam:\n  - {name: host, value: plsparcrasb010.lnf.infn.it}\n",
    "thor-rasb-las015": "name: thor-rasb-las015\nserver: plsparcrasb010.lnf.infn.it\nport: 4015\n"
    "iocparam:\n  - {name: host, value: plsparcrasb010.lnf.infn.it}\n",
    "haz-tcp-sbn401": "name: haz-tcp-SBN401\n",
}


def _wl(name, *, app="auto", instance=None, replicas=1, kind="StatefulSet"):
    return IocWorkload(
        kind=kind, name=name, namespace="sparc", instance=instance or name, replicas=replicas,
        ready_replicas=replicas, created=datetime(2026, 1, 1),
        argocd_app=f"sparc-{name}-ioc" if app == "auto" else app,
    )


class FakeKube:
    def __init__(self, workloads, apps=None, apps_error=None, configs=None):
        self.workloads = {w.name: w for w in workloads}
        self.apps, self.apps_error = apps, apps_error
        self.configs = CONFIGS if configs is None else configs
        self.scaled: list[tuple] = []
        self.deleted: list[tuple] = []

    async def list_ioc_workloads(self, namespace=None):
        return list(self.workloads.values())

    async def get_ioc_workload(self, kind, name, namespace):
        w = self.workloads.get(name)
        return w if w and w.kind == kind else None

    async def list_ioc_configs(self, namespace=None):
        return self.configs

    async def list_argocd_application_names(self, ns):
        if self.apps_error:
            raise self.apps_error
        return self.apps

    async def scale_ioc_workload(self, kind, name, namespace, replicas):
        self.scaled.append((kind, name, replicas))

    async def delete_ioc_workload(self, kind, name, namespace):
        self.deleted.append((kind, name))


class FakeKnowledge:
    def __init__(self, content=VALUES, fail=False):
        self.content, self.fail = content, fail

    async def get_file(self, repo, path, ref):
        if self.fail:
            from argus.core.errors import ArgusError

            raise ArgusError("gitlab down")

        class File:
            content = self.content

        return File()


MANAGED_APPS = {"sparc-sbncamera-02-ioc", "sparc-thor-rasb-las001-ioc", "sparc-haz-tcp-sbn401-ioc"}


def _service(kube, knowledge=None):
    return LeftoverService(kubernetes=kube, knowledge=knowledge or FakeKnowledge(), default_namespace="sparc")


async def test_orphan_competing_for_a_camera_is_critical():
    kube = FakeKube([_wl("sbncamera-02"), _wl("sbn01")], apps=MANAGED_APPS)
    report = (await _service(kube).find()).to_dict()
    [left] = report["leftovers"]
    assert (left["name"], left["state"], left["removable"]) == ("sbn01", "orphan", True)
    assert left["conflicts"] == [{"ioc": "sbncamera-02", "device": "192.168.110.41"}]
    assert report["findings"][0]["severity"] == "critical"
    assert report["managed_iocs"] == 1


async def test_shared_converter_on_different_ports_is_not_a_conflict():
    kube = FakeKube([_wl("thor-rasb-las001"), _wl("thor-rasb-las015")], apps=MANAGED_APPS)
    report = (await _service(kube).find()).to_dict()
    [left] = report["leftovers"]
    assert left["conflicts"] == []
    assert report["findings"][0]["severity"] == "warning"


async def test_uppercase_ioc_name_and_lowercase_instance_is_managed():
    kube = FakeKube([_wl("haz-tcp-sbn401")], apps=MANAGED_APPS)
    report = (await _service(kube).find()).to_dict()
    assert report["leftovers"] == []
    assert "No leftover" in report["findings"][0]["message"]


async def test_hand_deployed_not_in_config_is_unmanaged():
    kube = FakeKube([_wl("sbncam02", app=None)], apps=MANAGED_APPS)
    [left] = (await _service(kube).find()).to_dict()["leftovers"]
    assert (left["state"], left["removable"]) == ("unmanaged", True)


async def test_scaled_to_zero_leftover_is_info():
    kube = FakeKube([_wl("sbncamera-02"), _wl("sbn01", replicas=0)], apps=MANAGED_APPS)
    report = (await _service(kube).find()).to_dict()
    assert report["findings"][0]["severity"] == "info"


async def test_without_argocd_access_only_suspects_and_never_removes():
    kube = FakeKube([_wl("sbn01")], apps_error=KubernetesUnavailableError("forbidden"))
    service = _service(kube)
    report = (await service.find()).to_dict()
    [left] = report["leftovers"]
    assert (left["state"], left["removable"]) == ("suspected", False)
    assert report["sources"]["argocd_applications"].startswith("unavailable")
    with pytest.raises(LeftoverError):
        await service.remove("sbn01")
    assert kube.scaled == [] and kube.deleted == []


async def test_managed_app_in_cluster_but_missing_from_config_is_managed():
    # e.g. a softioc whose app exists, defined outside epicsConfiguration.iocs
    kube = FakeKube([_wl("check-motor-movement")], apps=MANAGED_APPS | {"sparc-check-motor-movement-ioc"})
    assert (await _service(kube).find()).to_dict()["leftovers"] == []


async def test_remove_scales_to_zero_by_default_and_gives_undo():
    kube = FakeKube([_wl("sbncamera-02"), _wl("sbn01")], apps=MANAGED_APPS)
    result = await _service(kube).remove("sbn01")
    assert kube.scaled == [("StatefulSet", "sbn01", 0)] and kube.deleted == []
    assert result["undo"] == "kubectl -n sparc scale statefulset sbn01 --replicas=1"


async def test_remove_delete():
    kube = FakeKube([_wl("sbn01")], apps=MANAGED_APPS)
    await _service(kube).remove("sbn01", action="delete")
    assert kube.deleted == [("StatefulSet", "sbn01")]


async def test_remove_refuses_managed_ioc():
    kube = FakeKube([_wl("sbncamera-02")], apps=MANAGED_APPS)
    with pytest.raises(LeftoverError, match="managed"):
        await _service(kube).remove("sbncamera-02", action="delete")
    assert kube.deleted == []


async def test_remove_refuses_unknown_workload_and_bad_action():
    kube = FakeKube([], apps=MANAGED_APPS)
    with pytest.raises(LeftoverError):
        await _service(kube).remove("nothing")
    with pytest.raises(ValidationError):
        await _service(kube).remove("sbn01", action="nuke")


async def test_unmanaged_needs_readable_config_to_be_removable():
    kube = FakeKube([_wl("sbncam02", app=None)], apps=MANAGED_APPS)
    service = _service(kube, FakeKnowledge(fail=True))
    [left] = (await service.find()).to_dict()["leftovers"]
    assert (left["state"], left["removable"]) == ("suspected", False)
    with pytest.raises(LeftoverError):
        await service.remove("sbncam02")
