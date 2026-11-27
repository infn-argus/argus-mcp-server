from types import SimpleNamespace

import pytest

from argus.config.settings import KubernetesSettings
from argus.providers.kubernetes.exceptions import KubernetesUnconfiguredError
from argus.providers.kubernetes.kubernetes import KubernetesProvider


def _fake_pod(name="ioc-qf12-abcde", namespace="accelerator", phase="Running", ready="True", restarts=0):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        status=SimpleNamespace(
            phase=phase,
            conditions=[SimpleNamespace(type="Ready", status=ready)],
            container_statuses=[SimpleNamespace(restart_count=restarts)],
            start_time=None,
        ),
        spec=SimpleNamespace(node_name="node-1"),
    )


def test_unconfigured_when_no_kubeconfig_and_not_in_cluster(monkeypatch):
    import kubernetes.config

    def _raise(*a, **k):
        raise kubernetes.config.ConfigException("no config found")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", _raise)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", _raise)

    provider = KubernetesProvider(KubernetesSettings(_env_file=None))
    assert provider.is_configured() is False


async def test_require_configured_raises_clean_error(monkeypatch):
    import kubernetes.config

    def _raise(*a, **k):
        raise kubernetes.config.ConfigException("no config found")

    monkeypatch.setattr(kubernetes.config, "load_incluster_config", _raise)
    monkeypatch.setattr(kubernetes.config, "load_kube_config", _raise)

    provider = KubernetesProvider(KubernetesSettings(_env_file=None))
    with pytest.raises(KubernetesUnconfiguredError):
        await provider.list_iocs()


async def test_list_iocs_converts_pods(monkeypatch):
    monkeypatch.setattr("kubernetes.config.load_incluster_config", lambda: None)

    provider = KubernetesProvider(KubernetesSettings(_env_file=None))
    assert provider.is_configured() is True

    fake_api = SimpleNamespace(
        list_namespaced_pod=lambda ns, label_selector=None: SimpleNamespace(items=[_fake_pod()])
    )
    provider._api = fake_api

    pods = await provider.list_iocs(namespace="accelerator")
    assert pods[0].name == "ioc-qf12-abcde"
    assert pods[0].ready is True
    assert pods[0].phase == "Running"


# ---- pod detail, events, nodes (cluster diagnostics) ------------------------

import json  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from argus.providers.kubernetes.exceptions import KubernetesResourceNotFoundError  # noqa: E402
from argus.providers.kubernetes.kubernetes import NETWORK_STATUS_ANNOTATION  # noqa: E402

_NETWORK_STATUS = json.dumps(
    [
        {"name": "k8s-pod-network", "ips": ["10.42.30.20"], "default": True},
        {"name": "sparc/sparc-br-cams", "interface": "net1", "ips": ["192.168.110.212"], "mac": "aa:bb"},
    ]
)


def _configured_provider(monkeypatch, api) -> KubernetesProvider:
    monkeypatch.setattr("kubernetes.config.load_incluster_config", lambda: None)
    provider = KubernetesProvider(KubernetesSettings(_env_file=None))
    provider._api = api
    return provider


def _detailed_pod():
    term = SimpleNamespace(reason="OOMKilled", exit_code=137, finished_at=datetime(2026, 11, 26, tzinfo=timezone.utc))
    expr = SimpleNamespace(key="kubernetes.io/hostname", operator="In", values=["plk8sdadom003.lnf.infn.it"])
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="sbncamera-03-0",
            namespace="sparc",
            annotations={NETWORK_STATUS_ANNOTATION: _NETWORK_STATUS},
            owner_references=[SimpleNamespace(kind="StatefulSet", name="sbncamera-03")],
        ),
        status=SimpleNamespace(
            phase="Running",
            conditions=[SimpleNamespace(type="Ready", status="True"), SimpleNamespace(type="PodScheduled", status="True")],
            container_statuses=[
                SimpleNamespace(
                    name="ioc",
                    image="ghcr.io/infn-epics/ioc:1",
                    ready=True,
                    restart_count=2,
                    state=SimpleNamespace(running=object(), waiting=None, terminated=None),
                    last_state=SimpleNamespace(terminated=term),
                )
            ],
            pod_ip="10.42.30.20",
            host_ip="192.168.108.14",
            start_time=None,
        ),
        spec=SimpleNamespace(
            node_name="plk8sdadom003.lnf.infn.it",
            host_network=None,
            node_selector={"control": "sparc-br-cams"},
            affinity=SimpleNamespace(
                node_affinity=SimpleNamespace(
                    required_during_scheduling_ignored_during_execution=SimpleNamespace(
                        node_selector_terms=[SimpleNamespace(match_expressions=[expr])]
                    )
                )
            ),
        ),
    )


async def test_get_pod_parses_networks_containers_and_placement(monkeypatch):
    api = SimpleNamespace(read_namespaced_pod=lambda name, ns: _detailed_pod())
    detail = await _configured_provider(monkeypatch, api).get_pod("sbncamera-03-0", "sparc")

    assert detail.node == "plk8sdadom003.lnf.infn.it"
    assert detail.owner == "StatefulSet/sbncamera-03"
    net1 = next(n for n in detail.networks if n.interface == "net1")
    assert net1.name == "sparc/sparc-br-cams" and net1.ips == ["192.168.110.212"]
    assert detail.containers[0].state == "running"
    assert detail.containers[0].last_termination.startswith("OOMKilled (exit 137) at 2026-11-26")
    assert detail.placement == ["control=sparc-br-cams", "kubernetes.io/hostname In ['plk8sdadom003.lnf.infn.it']"]
    assert detail.conditions_not_ok == []


async def test_get_pod_missing_is_not_found(monkeypatch):
    class _NotFound(Exception):
        status = 404

    def _raise(name, ns):
        raise _NotFound()

    api = SimpleNamespace(read_namespaced_pod=_raise)
    with pytest.raises(KubernetesResourceNotFoundError):
        await _configured_provider(monkeypatch, api).get_pod("nope-0", "sparc")


async def test_names_are_validated_before_field_selectors(monkeypatch):
    called = []
    api = SimpleNamespace(list_pod_for_all_namespaces=lambda **kw: called.append(kw))
    provider = _configured_provider(monkeypatch, api)
    with pytest.raises(KubernetesResourceNotFoundError):
        await provider.list_pods_on_node("dom003,metadata.namespace=kube-system")
    assert called == []


async def test_list_events_newest_first_with_object_filter(monkeypatch):
    seen = {}

    def _events(ns, **kw):
        seen.update(kw, ns=ns)
        mk = lambda reason, ts: SimpleNamespace(  # noqa: E731
            type="Warning",
            reason=reason,
            message=f" {reason} happened ",
            count=None,
            last_timestamp=ts,
            metadata=SimpleNamespace(creation_timestamp=None),
            involved_object=SimpleNamespace(kind="Pod", name="sbncamera-03-0"),
        )
        return SimpleNamespace(
            items=[mk("Old", datetime(2026, 1, 1, tzinfo=timezone.utc)), mk("New", datetime(2026, 2, 1, tzinfo=timezone.utc))]
        )

    provider = _configured_provider(monkeypatch, SimpleNamespace(list_namespaced_event=_events))
    events = await provider.list_events("sparc", object_name="sbncamera-03-0")
    assert seen == {"ns": "sparc", "field_selector": "involvedObject.name=sbncamera-03-0"}
    assert [e.reason for e in events] == ["New", "Old"]
    assert events[0].message == "New happened" and events[0].count == 1


async def test_get_node_reports_problems_and_filters_noisy_labels(monkeypatch):
    node = SimpleNamespace(
        metadata=SimpleNamespace(
            name="plk8sdadom005",
            labels={
                "control": "sparc-br-cams",
                "kubernetes.io/hostname": "plk8sdadom005",
                "feature.node.kubernetes.io/cpu-cpuid.AVX2": "true",
                "beta.kubernetes.io/os": "linux",
            },
        ),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type="Ready", status="True"),
                SimpleNamespace(type="MemoryPressure", status="True"),
                SimpleNamespace(type="DiskPressure", status="False"),
            ],
            addresses=[SimpleNamespace(type="InternalIP", address="192.168.108.17")],
            node_info=SimpleNamespace(kubelet_version="v1.33.1", kernel_version="6.8.0", os_image="Ubuntu"),
            capacity={"cpu": "32"},
            allocatable={"cpu": "31"},
        ),
        spec=SimpleNamespace(unschedulable=None, taints=[SimpleNamespace(key="gpu", value=None, effect="NoSchedule")]),
    )
    info = await _configured_provider(monkeypatch, SimpleNamespace(read_node=lambda n: node)).get_node("plk8sdadom005")
    assert info.ready is True
    assert info.problems == ["MemoryPressure=True"]
    assert info.labels == {"control": "sparc-br-cams", "kubernetes.io/hostname": "plk8sdadom005"}
    assert info.taints == ["gpu=:NoSchedule"]
    assert info.internal_ip == "192.168.108.17"


async def test_list_pods_on_node_marks_extra_networks(monkeypatch):
    seen = {}
    plain = _fake_pod(name="gateway-0", namespace="sparc")
    plain.metadata.annotations = {}

    def _list(**kw):
        seen.update(kw)
        return SimpleNamespace(items=[_detailed_pod(), plain])

    provider = _configured_provider(monkeypatch, SimpleNamespace(list_pod_for_all_namespaces=_list))
    pods = await provider.list_pods_on_node("plk8sdadom003.lnf.infn.it")
    assert seen == {"field_selector": "spec.nodeName=plk8sdadom003.lnf.infn.it"}
    by_name = {p.name: p for p in pods}
    assert by_name["sbncamera-03-0"].extra_networks == ["sparc/sparc-br-cams"]
    assert by_name["gateway-0"].extra_networks == []


async def test_list_iocs_reports_the_ioc_name_from_its_label(monkeypatch):
    monkeypatch.setattr("kubernetes.config.load_incluster_config", lambda: None)
    provider = KubernetesProvider(KubernetesSettings(
        _env_file=None, K8S_IOC_LABEL_SELECTOR_TEMPLATE="app.kubernetes.io/instance={ioc_name}"))
    pod = _fake_pod(name="fi1-cam-vis-01-0")
    pod.metadata.labels = {"app.kubernetes.io/instance": "fi1-cam-vis-01"}
    provider._api = SimpleNamespace(list_namespaced_pod=lambda ns, label_selector=None: SimpleNamespace(items=[pod]))
    [status] = await provider.list_iocs(namespace="euaps")
    assert (status.name, status.ioc) == ("fi1-cam-vis-01-0", "fi1-cam-vis-01")


async def test_get_pod_for_ioc_also_takes_the_pod_name(monkeypatch):
    monkeypatch.setattr("kubernetes.config.load_incluster_config", lambda: None)
    provider = KubernetesProvider(KubernetesSettings(
        _env_file=None, K8S_IOC_LABEL_SELECTOR_TEMPLATE="app.kubernetes.io/instance={ioc_name}"))
    pod = _fake_pod(name="fi1-cam-vis-01-0")
    pod.metadata.labels = {"app.kubernetes.io/instance": "fi1-cam-vis-01"}

    def read_namespaced_pod(name, ns):
        if name != "fi1-cam-vis-01-0":
            raise RuntimeError("404")
        return pod

    provider._api = SimpleNamespace(
        list_namespaced_pod=lambda ns, label_selector=None: SimpleNamespace(items=[]),  # no IOC by that name
        read_namespaced_pod=read_namespaced_pod,
    )
    found = await provider.get_pod_for_ioc("fi1-cam-vis-01-0", "euaps")
    assert found.ioc == "fi1-cam-vis-01"
    assert await provider.get_pod_for_ioc("nothing-0", "euaps") is None
