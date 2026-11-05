"""pvAccess first, Channel Access as fallback, behind the EpicsProvider protocol.

Reads simply try the other protocol on any EPICS error. Writes are stricter,
because a write that timed out may still have taken effect: the PV is first
read over pvAccess, and if it answers, the write goes over pvAccess only and
any failure is returned rather than retried. Channel Access is used only when
the PV didn't answer that read, so nothing was sent over pvAccess. A PV can
therefore never be written over both protocols by one call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, TypeVar

from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.epics.exceptions import (
    EpicsConnectionError,
    EpicsError,
    EpicsTimeoutError,
    EpicsUnconfiguredError,
)
from argus.providers.epics.interface import EpicsProvider
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult

T = TypeVar("T")


class FallbackEpicsProvider:
    name: ClassVar[str] = "epics"

    def __init__(self, primary: EpicsProvider | None, secondary: EpicsProvider | None) -> None:
        self._providers = [p for p in (primary, secondary) if p is not None]

    def _configured(self) -> list[EpicsProvider]:
        return [p for p in self._providers if p.is_configured()]

    def is_configured(self) -> bool:
        return bool(self._configured())

    async def health(self) -> ProviderHealth:
        configured = self._configured()
        if not configured:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        healths = [await p.health() for p in configured]
        if any(h.status == ProviderStatus.OK for h in healths):
            return ProviderHealth(name=self.name, status=ProviderStatus.OK)
        return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail="; ".join(str(h.detail) for h in healths))

    def _require_configured(self) -> list[EpicsProvider]:
        configured = self._configured()
        if not configured:
            raise EpicsUnconfiguredError("EPICS is not configured (set EPICS_PVA_ADDR_LIST and/or EPICS_CA_ADDR_LIST).")
        return configured

    async def _first_success(self, call: Callable[[EpicsProvider], Awaitable[T]]) -> T:
        configured = self._require_configured()
        errors: list[EpicsError] = []
        for provider in configured:
            try:
                return await call(provider)
            except EpicsError as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        kind = EpicsTimeoutError if all(isinstance(e, EpicsTimeoutError) for e in errors) else EpicsConnectionError
        raise kind("; ".join(str(e) for e in errors)) from errors[-1]

    async def get(self, pv_name: str, timeout: float = 5.0) -> PVValue:
        return await self._first_success(lambda p: p.get(pv_name, timeout=timeout))

    async def info(self, pv_name: str, timeout: float = 5.0) -> PVInfo:
        return await self._first_success(lambda p: p.info(pv_name, timeout=timeout))

    async def put(self, pv_name: str, value: Any, timeout: float = 5.0) -> PVWriteResult:
        configured = self._require_configured()
        for provider in configured[:-1]:
            try:
                await provider.get(pv_name, timeout=timeout)
            except EpicsError:
                continue  # it doesn't answer here, and nothing was written
            return await provider.put(pv_name, value, timeout=timeout)
        return await configured[-1].put(pv_name, value, timeout=timeout)

    async def get_many(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]:
        configured = self._require_configured()
        # Callers bound the whole batch by this timeout, so the attempts share it.
        share = timeout / len(configured)
        values = await configured[0].get_many(pv_names, timeout=share)
        for provider in configured[1:]:
            missing = [v.name for v in values if v.status == "disconnected"]
            if not missing:
                break
            found = {v.name: v for v in await provider.get_many(missing, timeout=share)}
            values = [found.get(v.name, v) for v in values]
        return values
