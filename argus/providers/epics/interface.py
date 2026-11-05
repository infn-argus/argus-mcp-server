"""Common protocol implemented by both the Channel Access and pvAccess adapters.

Callers depend on this Protocol, not on either concrete adapter. fallback.py
implements it too, trying pvAccess first and Channel Access if that fails.
"""

from __future__ import annotations

from typing import Any, Protocol

from argus.providers.base import Provider
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult


class EpicsProvider(Provider, Protocol):
    async def get(self, pv_name: str, timeout: float = 5.0) -> PVValue: ...

    async def put(self, pv_name: str, value: Any, timeout: float = 5.0) -> PVWriteResult: ...

    async def info(self, pv_name: str, timeout: float = 5.0) -> PVInfo: ...

    async def get_many(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]: ...
