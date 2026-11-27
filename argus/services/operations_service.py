"""Operational actions: reading/writing PVs, searching channels, IOC lifecycle.

Business logic lives here, not in the MCP tool handlers or the providers —
providers only retrieve/mutate data, this service decides *how* (e.g. which
EPICS transport to use) and combines results into what a tool needs.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any

from argus.providers.channelfinder.channelfinder import ChannelFinderProvider
from argus.providers.channelfinder.exceptions import ChannelFinderError
from argus.providers.channelfinder.models import CFChannel
from argus.providers.epics.exceptions import EpicsError
from argus.providers.epics.fallback import FallbackEpicsProvider
from argus.providers.epics.interface import EpicsProvider
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult
from argus.providers.kubernetes.exceptions import KubernetesError
from argus.providers.kubernetes.kubernetes import KubernetesProvider
from argus.providers.kubernetes.models import PodStatus
from argus.providers.logbook.exceptions import LogbookError
from argus.providers.logbook.logbook import LogbookProvider
from argus.core.cache import AsyncTTLCache


class ProcedureRegistry:
    """Named, pre-approved multi-step operational procedures.

    Intentionally empty for now: the spec calls for an execute_procedure tool
    but never defines procedure semantics or safety constraints. Wiring exists
    end-to-end; defining real procedures is a follow-up requiring domain and
    safety sign-off, not something to guess at here.
    """

    def __init__(self) -> None:
        self._procedures: dict[str, Any] = {}

    def list_procedures(self) -> list[str]:
        return sorted(self._procedures)

    async def execute(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if name not in self._procedures:
            raise NotImplementedError(
                f"Procedure '{name}' is not defined. No operational procedures are "
                "registered yet — this tool is scaffolded pending domain sign-off."
            )
        return await self._procedures[name](params)


@dataclass
class MachineSummary:
    ioc_count: int
    healthy_iocs: int
    unhealthy_iocs: int
    recent_alarm_count: int


def _level_pattern(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^:]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^:]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


class OperationsService:
    def __init__(
        self,
        epics_ca: EpicsProvider,
        epics_pva: EpicsProvider | None = None,
        channelfinder: ChannelFinderProvider | None = None,
        kubernetes: KubernetesProvider | None = None,
        logbook: LogbookProvider | None = None,
        procedures: ProcedureRegistry | None = None,
        kubernetes_cache: AsyncTTLCache | None = None,
    ) -> None:
        self._epics = FallbackEpicsProvider(primary=epics_pva, secondary=epics_ca)
        self._channelfinder = channelfinder
        self._kubernetes = kubernetes
        self._logbook = logbook
        self._procedures = procedures or ProcedureRegistry()
        self._kubernetes_cache = kubernetes_cache or AsyncTTLCache(ttl=30)

    async def get_pv(self, pv_name: str) -> PVValue:
        return await self._epics.get(pv_name)

    async def get_pvs(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]:
        """Many PVs in one batch; one that doesn't answer comes back disconnected, never fails the rest."""
        return await self._epics.get_many(pv_names, timeout=timeout)

    async def pv_names_matching(self, pattern: str, limit: int) -> tuple[list[str], bool]:
        """PV names ChannelFinder lists for a name pattern, sorted; and whether there were
        more than `limit`. `*` matches within one ':'-separated level (EUAPS:CAM:*:ArrayRate_RBV
        is each camera's rate, not every plugin's), `**` across levels, `?` one character.
        ChannelFinder's own `*` crosses levels, so it is asked broadly and filtered here."""
        if self._channelfinder is None or not self._channelfinder.is_configured():
            raise ChannelFinderError(
                "A PV pattern needs ChannelFinder (set CHANNELFINDER_BASE_URL): pass pv_names instead."
            )
        channels = await self._channelfinder.find_channels(pattern.replace("**", "*"))
        matcher = _level_pattern(pattern)
        names = sorted({c.name for c in channels if matcher.fullmatch(c.name)})
        return names[:limit], len(names) > limit

    async def set_pv(self, pv_name: str, value: Any) -> PVWriteResult:
        return await self._epics.put(pv_name, value)

    async def get_pv_info(self, pv_name: str) -> PVInfo:
        return await self._epics.info(pv_name)

    async def search_pvs(self, query: str, limit: int = 50) -> list[CFChannel]:
        if self._channelfinder is None or not self._channelfinder.is_configured():
            raise ChannelFinderError(
                "search_pvs requires ChannelFinder (set CHANNELFINDER_BASE_URL)."
            )
        channels = await self._channelfinder.find_channels(query)
        return channels[:limit]

    async def list_iocs(self, namespace: str | None = None) -> list[PodStatus]:
        if self._kubernetes is None or not self._kubernetes.is_configured():
            raise KubernetesError("list_iocs requires Kubernetes (set KUBECONFIG or run in-cluster).")
        cache_key = f"iocs:{namespace or 'default'}"
        return await self._kubernetes_cache.get_or_set(
            cache_key, lambda: self._kubernetes.list_iocs(namespace=namespace)
        )

    async def restart_ioc(self, pod_name: str, namespace: str) -> bool:
        if self._kubernetes is None or not self._kubernetes.is_configured():
            raise KubernetesError("restart_ioc requires Kubernetes (set KUBECONFIG or run in-cluster).")
        result = await self._kubernetes.restart_pod(pod_name, namespace)
        self._kubernetes_cache.invalidate(f"iocs:{namespace}")
        return result

    async def execute_procedure(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._procedures.execute(name, params)

    async def machine_summary(self, namespace: str | None = None) -> MachineSummary:
        iocs = await self.list_iocs(namespace=namespace)
        healthy = sum(1 for pod in iocs if pod.ready and pod.phase == "Running")

        recent_alarm_count = 0
        if self._logbook is not None and self._logbook.is_configured():
            from datetime import datetime, timedelta, timezone

            try:
                entries = await self._logbook.search_entries(
                    tags=["alarm"], since=datetime.now(timezone.utc) - timedelta(hours=24), limit=200
                )
                recent_alarm_count = len(entries)
            except LogbookError:
                recent_alarm_count = 0

        return MachineSummary(
            ioc_count=len(iocs),
            healthy_iocs=healthy,
            unhealthy_iocs=len(iocs) - healthy,
            recent_alarm_count=recent_alarm_count,
        )
