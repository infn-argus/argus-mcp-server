from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from argus.core.models import ProviderResult


@dataclass
class Device:
    """The fundamental ARGUS object — not a PV. Assembled by device_service
    from ChannelFinder metadata (the device/PV registry source of truth).
    """

    name: str
    device_class: str | None = None
    pv_names: list[str] = field(default_factory=list)
    primary_pv: str | None = None
    ioc_name: str | None = None
    pod_name: str | None = None
    namespace: str | None = None
    argocd_app: str | None = None
    git_repo: str | None = None
    rack: str | None = None
    owner: str | None = None
    network_location: str | None = None
    documentation_refs: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)


@dataclass
class DeviceStatusReport:
    device: Device
    live_pvs: ProviderResult
    pod: ProviderResult
    generated_at: datetime
    degraded_providers: list[str] = field(default_factory=list)
    # Large devices (a camera has ~1000 PVs): only the status PVs are read.
    pvs_total: int | None = None
    note: str | None = None


@dataclass
class DiagnosticReport:
    device: Device
    live_pvs: ProviderResult
    history: ProviderResult
    pod: ProviderResult
    pod_logs: ProviderResult
    argocd: ProviderResult
    logbook: ProviderResult
    documentation: ProviderResult
    generated_at: datetime
    degraded_providers: list[str] = field(default_factory=list)
    pvs_total: int | None = None
    note: str | None = None
