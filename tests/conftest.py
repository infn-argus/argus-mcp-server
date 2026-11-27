from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from argus.config.settings import (
    ArchiverSettings,
    ArgoCDSettings,
    ChannelFinderSettings,
    DocumentationSettings,
    ElasticSettings,
    GitHubSettings,
    GitLabSettings,
    KubernetesSettings,
    LogbookSettings,
    LokiSettings,
    PrometheusSettings,
    SaveRestoreSettings,
    Settings,
)
from argus.core.cache import AsyncTTLCache
from argus.core.context import AppContext
from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.argocd.argocd import ArgoCDProvider
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.channelfinder.channelfinder import ChannelFinderProvider
from argus.providers.documentation.rag import LocalTfidfDocumentationProvider
from argus.providers.elastic.elasticsearch import ElasticsearchProvider
from argus.providers.epics.exceptions import EpicsConnectionError, EpicsUnconfiguredError
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult
from argus.providers.git.github import GitHubProvider
from argus.providers.git.gitlab import GitLabProvider
from argus.providers.kubernetes.kubernetes import KubernetesProvider
from argus.providers.logbook.logbook import LogbookProvider
from argus.providers.loki.loki import LokiProvider
from argus.providers.prometheus.prometheus import PrometheusProvider
from argus.providers.saverestore.saverestore import SaveRestoreProvider
from argus.services.cluster_service import ClusterService
from argus.services.connectivity_service import ConnectivityService
from argus.services.device_service import DeviceService
from argus.services.diagnostics_service import DiagnosticsService
from argus.services.documentation_service import DocumentationService
from argus.services.history_service import HistoryService
from argus.services.beamline_inventory_service import BeamlineInventoryService
from argus.services.knowledge_service import KnowledgeService
from argus.services.operations_service import OperationsService, ProcedureRegistry
from argus.services.saverestore_service import SaveRestoreService


@dataclass
class FakeEpicsProvider:
    """In-memory EpicsProvider test double, avoids needing a real IOC."""

    name: ClassVar[str] = "fake_epics"
    values: dict[str, Any] = field(default_factory=dict)
    configured: bool = True

    def is_configured(self) -> bool:
        return self.configured

    async def health(self) -> ProviderHealth:
        status = ProviderStatus.OK if self.configured else ProviderStatus.UNCONFIGURED
        return ProviderHealth(name=self.name, status=status)

    async def get(self, pv_name: str, timeout: float = 5.0) -> PVValue:
        if not self.configured:
            raise EpicsUnconfiguredError("fake epics not configured")
        if pv_name not in self.values:
            raise EpicsConnectionError(f"PV '{pv_name}' not found")
        return PVValue(name=pv_name, value=self.values[pv_name])

    async def put(self, pv_name: str, value: Any, timeout: float = 5.0) -> PVWriteResult:
        if not self.configured:
            raise EpicsUnconfiguredError("fake epics not configured")
        self.values[pv_name] = value
        return PVWriteResult(name=pv_name, accepted=True, new_value=value)

    async def info(self, pv_name: str, timeout: float = 5.0) -> PVInfo:
        if not self.configured:
            raise EpicsUnconfiguredError("fake epics not configured")
        return PVInfo(name=pv_name, native_type="double")

    async def get_many(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]:
        return [await self.get(name, timeout) for name in pv_names]


@pytest.fixture
def fake_epics() -> FakeEpicsProvider:
    return FakeEpicsProvider(values={"QF12:CURRENT": 4.2})


@pytest.fixture
def unconfigured_epics_pva() -> FakeEpicsProvider:
    return FakeEpicsProvider(configured=False)


@pytest.fixture
def app_context(fake_epics: FakeEpicsProvider, unconfigured_epics_pva: FakeEpicsProvider) -> AppContext:
    settings = Settings(_env_file=None)

    # Real (but unconfigured, since no env vars are set) provider instances —
    # exercises the genuine "unconfigured" degradation path, not a fake.
    archiver = ArchiverApplianceProvider(ArchiverSettings(_env_file=None))
    channelfinder = ChannelFinderProvider(ChannelFinderSettings(_env_file=None))
    kubernetes = KubernetesProvider(KubernetesSettings(_env_file=None))
    argocd = ArgoCDProvider(ArgoCDSettings(_env_file=None))
    logbook = LogbookProvider(LogbookSettings(_env_file=None))
    elastic = ElasticsearchProvider(ElasticSettings(_env_file=None))
    loki = LokiProvider(LokiSettings(_env_file=None))
    prometheus = PrometheusProvider(PrometheusSettings(_env_file=None))
    documentation = LocalTfidfDocumentationProvider(DocumentationSettings(_env_file=None))
    github = GitHubProvider(GitHubSettings(_env_file=None))
    gitlab = GitLabProvider(GitLabSettings(_env_file=None))
    saverestore = SaveRestoreProvider(SaveRestoreSettings(_env_file=None))

    device_cache = AsyncTTLCache(ttl=60)
    channelfinder_cache = AsyncTTLCache(ttl=60)
    kubernetes_cache = AsyncTTLCache(ttl=60)
    knowledge_cache = AsyncTTLCache(ttl=60)

    device_service = DeviceService(channelfinder=channelfinder, cache=device_cache)
    diagnostics_service = DiagnosticsService(
        device_service=device_service,
        epics=fake_epics,
        archiver=archiver,
        kubernetes=kubernetes,
        argocd=argocd,
        logbook=logbook,
        documentation=documentation,
        loki=loki,
    )
    operations_service = OperationsService(
        epics_ca=fake_epics,
        epics_pva=unconfigured_epics_pva,
        channelfinder=channelfinder,
        kubernetes=kubernetes,
        logbook=logbook,
        procedures=ProcedureRegistry(),
        kubernetes_cache=kubernetes_cache,
    )
    history_service = HistoryService(archiver=archiver, logbook=logbook, elastic=elastic, loki=loki)
    documentation_service = DocumentationService(documentation=documentation)
    knowledge_service = KnowledgeService(github=github, gitlab=gitlab, cache=knowledge_cache)
    beamline_inventory_service = BeamlineInventoryService(knowledge=knowledge_service)
    saverestore_service = SaveRestoreService(saverestore=saverestore)

    return AppContext(
        settings=settings,
        epics_ca=fake_epics,
        epics_pva=unconfigured_epics_pva,
        archiver=archiver,
        channelfinder=channelfinder,
        kubernetes=kubernetes,
        argocd=argocd,
        logbook=logbook,
        elastic=elastic,
        loki=loki,
        documentation=documentation,
        github=github,
        gitlab=gitlab,
        saverestore=saverestore,
        device_cache=device_cache,
        channelfinder_cache=channelfinder_cache,
        kubernetes_cache=kubernetes_cache,
        knowledge_cache=knowledge_cache,
        device_service=device_service,
        diagnostics_service=diagnostics_service,
        operations_service=operations_service,
        history_service=history_service,
        documentation_service=documentation_service,
        knowledge_service=knowledge_service,
        beamline_inventory_service=beamline_inventory_service,
        saverestore_service=saverestore_service,
        prometheus=prometheus,
        cluster_service=ClusterService(kubernetes=kubernetes, prometheus=prometheus),
        connectivity_service=ConnectivityService(
            kubernetes=kubernetes, knowledge=knowledge_service, default_namespace="default"
        ),
    )
