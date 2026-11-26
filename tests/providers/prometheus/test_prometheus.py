import httpx
import pytest
import respx

from argus.config.settings import PrometheusSettings
from argus.providers.prometheus.exceptions import (
    PrometheusNodeNotFoundError,
    PrometheusQueryError,
    PrometheusUnconfiguredError,
)
from argus.providers.prometheus.prometheus import PrometheusProvider

BASE = "http://prom.test"
INSTANCE = "192.168.108.14:9100"


def _provider() -> PrometheusProvider:
    return PrometheusProvider(PrometheusSettings(_env_file=None, PROMETHEUS_BASE_URL=BASE))


def _vector(*rows: tuple[dict, float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "status": "success",
            "data": {"resultType": "vector", "result": [{"metric": m, "value": [0, str(v)]} for m, v in rows]},
        },
    )


def _uname() -> httpx.Response:
    return _vector(
        ({"nodename": "plk8sdadom001", "instance": "192.168.108.10:9100"}, 1),
        ({"nodename": "plk8sdadom003.lnf.infn.it", "instance": INSTANCE}, 1),
    )


def _router(answers: dict[str, httpx.Response]):
    """Answer each query by the first key it contains; anything else gets an empty vector."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(httpx.QueryParams(request.url.query))["query"]
        handler.queries.append(query)
        for needle, response in answers.items():
            if needle in query:
                return response
        return _vector()

    handler.queries = []
    return handler


async def test_unconfigured_raises_clean_error():
    provider = PrometheusProvider(PrometheusSettings(_env_file=None))
    assert provider.is_configured() is False
    with pytest.raises(PrometheusUnconfiguredError):
        await provider.node_network("plk8sdadom003")


@respx.mock
async def test_resolves_fqdn_node_to_short_nodename_and_back():
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_router({"node_uname_info": _uname()}))
    provider = _provider()
    # Kubernetes FQDN -> node-exporter short name
    assert await provider.resolve_node_instance("plk8sdadom001.lnf.infn.it") == ("plk8sdadom001", "192.168.108.10:9100")
    # Kubernetes short name -> node-exporter FQDN
    assert await provider.resolve_node_instance("plk8sdadom003") == ("plk8sdadom003.lnf.infn.it", INSTANCE)


@respx.mock
async def test_unknown_node_raises_not_found():
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=_router({"node_uname_info": _uname()}))
    with pytest.raises(PrometheusNodeNotFoundError):
        await _provider().resolve_node_instance("nosuchnode")


async def test_invalid_names_never_reach_prometheus():
    provider = _provider()
    with pytest.raises(PrometheusQueryError):
        await provider.resolve_node_instance('dom003"} or vector(1) #')
    with pytest.raises(PrometheusQueryError):
        await provider.pod_network("sparc", 'x", namespace=~".+')


@respx.mock
async def test_node_network_builds_interfaces_and_counters():
    router = _router(
        {
            "node_uname_info": _uname(),
            "node_network_receive_bytes_total": _vector(({"device": "vlan110"}, 25e6), ({"device": "bond0"}, 30e6)),
            "node_network_receive_drop_total": _vector(({"device": "bond0"}, 1055.7), ({"device": "vlan110"}, 0)),
            "node_network_speed_bytes": _vector(({"device": "bond0"}, 2.5e9), ({"device": "vlan110"}, -1)),
            "node_network_mtu_bytes": _vector(({"device": "bond0"}, 1500)),
            "node_network_up": _vector(({"device": "bond0"}, 1), ({"device": "vlan110"}, 1), ({"device": "eno1"}, 0)),
            "node_netstat_Udp_RcvbufErrors": _vector(({}, 2.5)),
            "node_softnet_dropped_total": _vector(({}, 0.0)),
        }
    )
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=router)

    stats = await _provider().node_network("plk8sdadom003.lnf.infn.it", window_minutes=500)

    assert stats.instance == INSTANCE
    assert stats.window_minutes == 60  # clamped
    by_dev = {i.device: i for i in stats.interfaces}
    assert set(by_dev) == {"bond0", "vlan110"}  # eno1 is down and idle
    assert by_dev["bond0"].rx_bits_per_s == 240e6  # bytes -> bits
    assert by_dev["bond0"].speed_bits_per_s == 20e9
    assert by_dev["vlan110"].speed_bits_per_s is None  # -1: not reported
    assert by_dev["bond0"].rx_drops_per_s == pytest.approx(1055.7)
    assert stats.udp_rcvbuf_errors_per_s == 2.5
    # every query is pinned to the resolved instance, quoted, and skips virtual interfaces
    rate_q = next(q for q in router.queries if "node_network_receive_bytes_total" in q)
    assert f'instance="{INSTANCE}"' in rate_q
    assert 'device!~"lo|veth.*' in rate_q
    assert "[60m]" in rate_q


@respx.mock
async def test_node_cpu_percentages():
    router = _router(
        {
            "node_uname_info": _uname(),
            "count(count by (cpu)": _vector(({}, 32)),
            "max(rate(node_cpu_seconds_total": _vector(({}, 0.85)),
            "avg(rate(node_cpu_seconds_total{instance=\"192.168.108.14:9100\", mode=\"softirq\"}": _vector(({}, 0.04)),
            "node_load1": _vector(({}, 3.2)),
            "MemAvailable": _vector(({}, 0.42)),
        }
    )
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=router)
    cpu = await _provider().node_cpu("plk8sdadom003")
    assert cpu.cpus == 32
    assert cpu.max_cpu_softirq_percent == 85.0
    assert cpu.softirq_percent == 4.0
    assert cpu.memory_available_percent == 42.0
    assert cpu.load1 == 3.2


@respx.mock
async def test_pod_network_uses_cadvisor_interfaces():
    router = _router(
        {
            "container_network_receive_bytes_total": _vector(
                ({"interface": "net1", "node": "plk8sdadom003.lnf.infn.it"}, 3e6),
                ({"interface": "eth0", "node": "plk8sdadom003.lnf.infn.it"}, 1e4),
            ),
            "container_network_receive_packets_dropped_total": _vector(({"interface": "net1"}, 12.0)),
        }
    )
    respx.get(f"{BASE}/api/v1/query").mock(side_effect=router)
    net = await _provider().pod_network("sparc", "sbncamera-03-0")
    assert net.node == "plk8sdadom003.lnf.infn.it"
    by_dev = {i.device: i for i in net.interfaces}
    assert by_dev["net1"].rx_bits_per_s == 24e6
    assert by_dev["net1"].rx_drops_per_s == 12.0
    q = router.queries[0]
    assert q.startswith("max by (interface, node) (rate(")
    assert 'namespace="sparc", pod="sbncamera-03-0"' in q


@respx.mock
async def test_error_payload_raises_query_error():
    respx.get(f"{BASE}/api/v1/query").mock(
        return_value=httpx.Response(200, json={"status": "error", "error": "parse error"})
    )
    with pytest.raises(PrometheusQueryError):
        await _provider().pod_network("sparc", "p")
