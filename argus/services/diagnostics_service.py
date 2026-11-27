"""The flagship cross-provider composers: device_status() and diagnose_device().

Both resolve device metadata once (via device_service, cached), then fan out
to every relevant provider in parallel through gather_with_timeout, so one
slow or unavailable backend never blocks or breaks the whole response.
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import datetime, timedelta, timezone
from typing import Any

from argus.core.models import ProviderResult, gather_with_timeout, skipped
from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.argocd.argocd import ArgoCDProvider
from argus.providers.documentation.rag import LocalTfidfDocumentationProvider
from argus.providers.epics.interface import EpicsProvider
from argus.providers.kubernetes.kubernetes import KubernetesProvider
from argus.providers.logbook.logbook import LogbookProvider
from argus.providers.loki.loki import LokiProvider
from argus.services.device_service import DeviceService
from argus.services.models import Device, DeviceStatusReport, DiagnosticReport

_DEVICE_STATUS_TIMEOUT = 3.0
_DIAGNOSE_TIMEOUT = 10.0
_DIAGNOSE_HISTORY_POINTS = 60


class DiagnosticsService:
    def __init__(
        self,
        device_service: DeviceService,
        epics: EpicsProvider,
        archiver: ArchiverApplianceProvider,
        kubernetes: KubernetesProvider,
        argocd: ArgoCDProvider,
        logbook: LogbookProvider,
        documentation: LocalTfidfDocumentationProvider,
        loki: LokiProvider,
    ) -> None:
        self._device_service = device_service
        self._epics = epics
        self._archiver = archiver
        self._kubernetes = kubernetes
        self._argocd = argocd
        self._logbook = logbook
        self._documentation = documentation
        self._loki = loki

    async def _pod_logs(self, device: Device) -> Any:
        """Loki-first (7-day history, survives pod restarts) when configured,
        falling back to the live kubectl tail otherwise -- keeps beamlines
        without the platform's Loki deployed working exactly as before.
        """
        namespace = device.namespace or "default"
        if self._loki.is_configured():
            return await self._loki.search(namespace=namespace, pod=device.pod_name, limit=200)
        return await self._kubernetes.get_pod_logs(device.pod_name, namespace)

    async def device_status(self, device_name: str, *, timeout: float = _DEVICE_STATUS_TIMEOUT) -> DeviceStatusReport:
        device = await self._device_service.resolve_device(device_name)

        tasks: dict[str, Awaitable[Any]] = {"live_pvs": self._epics.get_many(device.pv_names, timeout=timeout)}
        pre_skipped: dict[str, ProviderResult] = {}

        if device.ioc_name:
            tasks["pod"] = self._kubernetes.get_pod_for_ioc(device.ioc_name, device.namespace)
        else:
            pre_skipped["pod"] = skipped("kubernetes", "device has no known IOC name")

        results = {**pre_skipped, **await gather_with_timeout(tasks, timeout=timeout)}

        return DeviceStatusReport(
            device=device,
            live_pvs=results["live_pvs"],
            pod=results["pod"],
            generated_at=datetime.now(timezone.utc),
            degraded_providers=[k for k, r in results.items() if r.status not in ("ok", "skipped")],
        )

    async def diagnose_device(
        self, device_name: str, *, history_window: timedelta = timedelta(hours=1), timeout: float = _DIAGNOSE_TIMEOUT
    ) -> DiagnosticReport:
        device = await self._device_service.resolve_device(device_name)
        now = datetime.now(timezone.utc)

        tasks: dict[str, Awaitable[Any]] = {"live_pvs": self._epics.get_many(device.pv_names, timeout=timeout)}
        pre_skipped: dict[str, ProviderResult] = {}

        if device.primary_pv:
            # A trend for context, not every sample: a busy PV logs thousands an hour.
            tasks["history"] = self._archiver.get_data(
                device.primary_pv, now - history_window, now, max_points=_DIAGNOSE_HISTORY_POINTS, timeout=timeout
            )
        else:
            pre_skipped["history"] = skipped("archiver", "device has no primary PV")

        if device.ioc_name:
            tasks["pod"] = self._kubernetes.get_pod_for_ioc(device.ioc_name, device.namespace)
        else:
            pre_skipped["pod"] = skipped("kubernetes", "device has no known IOC name")

        if device.pod_name:
            tasks["pod_logs"] = self._pod_logs(device)
        else:
            pre_skipped["pod_logs"] = skipped("kubernetes", "device has no known pod name")

        if device.argocd_app:
            tasks["argocd"] = self._argocd.get_application(device.argocd_app)
        else:
            pre_skipped["argocd"] = skipped("argocd", "device has no known ArgoCD application")

        tasks["logbook"] = self._logbook.search_entries(
            text=device.name, since=now - timedelta(days=7), timeout=timeout
        )
        tasks["documentation"] = self._documentation.search(device.name, top_k=3)

        results = {**pre_skipped, **await gather_with_timeout(tasks, timeout=timeout)}

        return DiagnosticReport(
            device=device,
            live_pvs=results["live_pvs"],
            history=results["history"],
            pod=results["pod"],
            pod_logs=results["pod_logs"],
            argocd=results["argocd"],
            logbook=results["logbook"],
            documentation=results["documentation"],
            generated_at=now,
            degraded_providers=[k for k, r in results.items() if r.status not in ("ok", "skipped")],
        )
