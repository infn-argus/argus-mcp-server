import asyncio

from argus.core.cache import AsyncTTLCache
from argus.providers.archiver.models import ArchiverSeries
from argus.providers.kubernetes.exceptions import KubernetesUnavailableError
from argus.services.device_service import DeviceService
from argus.services.diagnostics_service import DiagnosticsService


class _StubChannelFinder:
    """Configured, returns one channel with an ioc name so pod/pod_logs are
    actually attempted (not pre-skipped) in the tests below."""

    def is_configured(self) -> bool:
        return True

    async def find_channels(self, **kwargs):
        from argus.providers.channelfinder.models import CFChannel

        return [
            CFChannel(
                name="QF12:CURRENT",
                owner="ops",
                properties={
                    "device": "QF12",
                    "iocName": "ioc-qf12",
                    "k8sNamespace": "accelerator",
                    "k8sPod": "ioc-qf12-abc123",
                },
                tags=[],
            )
        ]


class _StubEpics:
    async def get_many(self, pv_names, timeout=5.0):
        from argus.providers.epics.models import PVValue

        return [PVValue(name=n, value=1.0) for n in pv_names]


class _StubArchiver:
    async def get_data(self, pv_name, start, end, timeout=5.0, **kwargs):
        return ArchiverSeries(pv_name=pv_name, samples=[])


class _StubKubernetesDown:
    async def get_pod_for_ioc(self, ioc_name, namespace):
        raise KubernetesUnavailableError("cluster unreachable")

    async def get_pod_logs(self, pod_name, namespace):
        raise KubernetesUnavailableError("cluster unreachable")


class _StubLokiUnconfigured:
    def is_configured(self) -> bool:
        return False

    async def search(self, **kwargs):
        raise AssertionError("should not be called when unconfigured")


class _StubLokiConfigured:
    def is_configured(self) -> bool:
        return True

    async def search(self, **kwargs):
        from datetime import datetime, timezone

        from argus.providers.loki.models import LokiLogEntry

        return [
            LokiLogEntry(
                timestamp=datetime.now(timezone.utc),
                namespace=kwargs.get("namespace") or "",
                pod=kwargs.get("pod") or "",
                container="",
                line="loki log line",
            )
        ]


class _StubArgoCD:
    async def get_application(self, app_name):
        return None


class _StubLogbook:
    async def search_entries(self, text=None, tags=None, since=None, timeout=5.0):
        return []


class _StubDocumentation:
    async def search(self, query, top_k=3):
        return []


def _build_service(epics=None, archiver=None, kubernetes=None, loki=None) -> DiagnosticsService:
    device_service = DeviceService(channelfinder=_StubChannelFinder(), cache=AsyncTTLCache(ttl=60))
    return DiagnosticsService(
        device_service=device_service,
        epics=epics or _StubEpics(),
        archiver=archiver or _StubArchiver(),
        kubernetes=kubernetes or _StubKubernetesDown(),
        argocd=_StubArgoCD(),
        logbook=_StubLogbook(),
        documentation=_StubDocumentation(),
        loki=loki or _StubLokiUnconfigured(),
    )


async def test_diagnose_device_degrades_when_kubernetes_down():
    service = _build_service()
    report = await service.diagnose_device("QF12", timeout=2)

    assert report.pod.status == "unavailable"
    # Loki unconfigured -> pod_logs falls back to the kubectl tail, which is
    # also down here, so it degrades too (not "skipped": the device DOES have
    # a known pod_name, see _StubChannelFinder's k8sPod).
    assert report.pod_logs.status == "unavailable"
    assert report.live_pvs.status == "ok"
    assert report.history.status == "ok"
    assert report.logbook.status == "ok"
    assert report.documentation.status == "ok"
    assert "pod" in report.degraded_providers
    assert "pod_logs" in report.degraded_providers
    assert "live_pvs" not in report.degraded_providers


async def test_diagnose_device_pod_logs_uses_loki_when_configured():
    service = _build_service(loki=_StubLokiConfigured())
    report = await service.diagnose_device("QF12", timeout=2)

    # _StubKubernetesDown.get_pod_logs always raises -- if pod_logs came from
    # kubectl instead of Loki, this would degrade instead of returning "ok".
    assert report.pod_logs.status == "ok"
    assert report.pod_logs.data[0].line == "loki log line"


async def test_diagnose_device_pod_logs_falls_back_to_kubectl_without_loki():
    class _KubernetesWithLogs:
        async def get_pod_for_ioc(self, ioc_name, namespace):
            return None

        async def get_pod_logs(self, pod_name, namespace):
            return "kubectl tail output"

    service = _build_service(kubernetes=_KubernetesWithLogs(), loki=_StubLokiUnconfigured())
    report = await service.diagnose_device("QF12", timeout=2)

    assert report.pod_logs.status == "ok"
    assert report.pod_logs.data == "kubectl tail output"


async def test_diagnose_device_respects_per_provider_timeout():
    class _SlowArchiver:
        async def get_data(self, pv_name, start, end, timeout=5.0, **kwargs):
            await asyncio.sleep(10)
            return ArchiverSeries(pv_name=pv_name, samples=[])

    service = _build_service(archiver=_SlowArchiver())

    started = asyncio.get_event_loop().time()
    report = await service.diagnose_device("QF12", timeout=0.05)
    elapsed = asyncio.get_event_loop().time() - started

    assert report.history.status == "timeout"
    assert report.live_pvs.status == "ok"
    assert elapsed < 5  # never blocked on the slow provider past its own timeout


async def test_diagnose_device_never_raises_on_total_provider_failure():
    class _BrokenEpics:
        async def get_many(self, pv_names, timeout=5.0):
            raise RuntimeError("totally broken")

    service = _build_service(epics=_BrokenEpics())
    report = await service.diagnose_device("QF12", timeout=2)
    assert report.live_pvs.status == "error"
