"""pvAccess provider, built on p4p's native asyncio client.

The first choice for every PV (see fallback.py), with Channel Access behind it.
Implements the same EpicsProvider protocol as the Channel Access adapter.

p4p's Context caches one channel per PV too, so a timeout always drops it
(Context.disconnect) and the next read opens a new one.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, ClassVar

from argus.config.settings import EpicsSettings
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.epics.exceptions import (
    EpicsConnectionError,
    EpicsTimeoutError,
    EpicsUnconfiguredError,
    PVWriteRejectedError,
)
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult


class PvAccessProvider:
    """EpicsProvider implementation backed by p4p (pvAccess protocol)."""

    name: ClassVar[str] = "epics_pva"

    def __init__(self, settings: EpicsSettings) -> None:
        self._settings = settings
        self._context: Any | None = None
        # PVs this process has opened a channel for, and not dropped since.
        self._cached: set[str] = set()

    def is_configured(self) -> bool:
        return bool(self._settings.pva_addr_list)

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            import p4p  # noqa: F401
        except ImportError as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))
        return ProviderHealth(name=self.name, status=ProviderStatus.OK)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise EpicsUnconfiguredError(
                "EPICS pvAccess is not configured (set EPICS_PVA_ADDR_LIST)."
            )

    def _get_context(self) -> Any:
        if self._context is None:
            import os

            from p4p.client.asyncio import Context

            if self._settings.pva_addr_list:
                os.environ.setdefault("EPICS_PVA_ADDR_LIST", self._settings.pva_addr_list)
            self._context = Context("pva")
        return self._context

    def _drop_channel(self, pv_name: str) -> None:
        self._cached.discard(pv_name)
        self._get_context().disconnect(pv_name)

    async def _read(self, pv_name: str, timeout: float, what: str) -> Any:
        """Read a PV; on a timeout drop the channel, and retry once if it was already cached."""
        ctx = self._get_context()
        was_cached = pv_name in self._cached
        self._cached.add(pv_name)
        for attempt in range(2):
            try:
                return await asyncio.wait_for(ctx.get(pv_name), timeout=timeout)
            except TimeoutError as exc:
                self._drop_channel(pv_name)
                if attempt or not was_cached:
                    raise EpicsTimeoutError(f"Timeout trying to {what} PV '{pv_name}' over pvAccess") from exc
                self._cached.add(pv_name)
            except Exception as exc:  # noqa: BLE001 - p4p raises various RemoteError subtypes
                self._drop_channel(pv_name)
                raise EpicsConnectionError(f"Failed to {what} PV '{pv_name}' over pvAccess: {exc}") from exc
        raise AssertionError("unreachable")

    async def get(self, pv_name: str, timeout: float = 5.0) -> PVValue:
        self._require_configured()
        return _to_pv_value(pv_name, await self._read(pv_name, timeout, "get"))

    async def put(self, pv_name: str, value: Any, timeout: float = 5.0) -> PVWriteResult:
        self._require_configured()
        ctx = self._get_context()
        try:
            await asyncio.wait_for(ctx.put(pv_name, value), timeout=timeout)
        except TimeoutError as exc:
            # Never retried: this write may still have taken effect.
            self._drop_channel(pv_name)
            raise EpicsTimeoutError(f"Timeout while setting PV '{pv_name}' over pvAccess") from exc
        except Exception as exc:  # noqa: BLE001
            self._drop_channel(pv_name)
            raise PVWriteRejectedError(f"Failed to set PV '{pv_name}' to {value!r} over pvAccess: {exc}") from exc

        return PVWriteResult(name=pv_name, accepted=True, new_value=value)

    async def info(self, pv_name: str, timeout: float = 5.0) -> PVInfo:
        self._require_configured()
        result = await self._read(pv_name, timeout, "get info for")
        value = _unwrap_value(result)
        raw = getattr(result, "raw", None)
        return PVInfo(
            name=pv_name,
            native_type="enum" if hasattr(result, "choice") else _plain_type_name(value),
            units=_get_field(result, "display.units") or None,
            precision=_get_field(result, "display.precision"),
            display_limits=_field_pair(result, "display.limitLow", "display.limitHigh"),
            alarm_limits=_field_pair(result, "valueAlarm.lowAlarmLimit", "valueAlarm.highAlarmLimit"),
            description=_get_field(result, "display.description") or None,
            choices=list(_get_field(result, "value.choices") or []) or None,
            alarm_message=_get_field(result, "alarm.message") or None,
            control_limits=_field_pair(result, "control.limitLow", "control.limitHigh"),
            warning_limits=_field_pair(result, "valueAlarm.lowWarningLimit", "valueAlarm.highWarningLimit"),
            details=_details(raw),
        )

    async def get_many(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]:
        self._require_configured()
        ctx = self._get_context()
        # p4p's own list-form ctx.get() gathers without return_exceptions=True,
        # so one failing PV would fail the whole batch -- gather individually
        # instead, so each PV degrades to "disconnected" on its own.
        results = await asyncio.gather(
            *(asyncio.wait_for(ctx.get(name), timeout=timeout) for name in pv_names),
            return_exceptions=True,
        )

        values: list[PVValue] = []
        for name, result in zip(pv_names, results, strict=True):
            if isinstance(result, Exception):
                self._drop_channel(name)  # no retry here: the caller's timeout bounds the whole batch
                values.append(PVValue(name=name, value=None, status="disconnected"))
            else:
                self._cached.add(name)
                values.append(_to_pv_value(name, result))
        return values


def _to_pv_value(name: str, result: Any) -> PVValue:
    return PVValue(
        name=name,
        value=_unwrap_value(result),
        timestamp=_extract_timestamp(result),
        severity=_extract_alarm_field(result, "severity"),
        status=_extract_alarm_field(result, "status"),
    )


def _unwrap_value(result: Any) -> Any:
    """A plain value from p4p's wrappers (ntfloat, ntint, ntstr...); an enum's label rather than its index."""
    choice = getattr(result, "choice", None)
    if choice is not None:
        return choice
    if hasattr(result, "tolist"):
        return result.tolist()  # waveforms
    for plain in (float, int, str):
        if isinstance(result, plain) and type(result) is not plain:
            return plain(result)
    try:
        return result.value if hasattr(result, "value") else result
    except Exception:  # noqa: BLE001
        return result


def _plain_type_name(value: Any) -> str:
    return type(value).__name__


def _extract_timestamp(result: Any) -> Any:
    return getattr(result, "timestamp", None)


def _extract_alarm_field(result: Any, field: str) -> str | None:
    value = getattr(result, field, None)
    return str(value) if value is not None else None


def _get_field(result: Any, path: str) -> Any:
    """A field of the full pvAccess structure, which p4p's wrappers keep in .raw."""
    try:
        return getattr(result, "raw", result)[path]
    except Exception:  # noqa: BLE001
        return None


def _details(raw: Any) -> dict[str, Any] | None:
    """The PV's structure as JSON can carry it, less the value itself (get_pv is for that, and it may be a waveform)."""
    if not hasattr(raw, "todict"):
        return None
    return _json_safe({k: v for k, v in raw.todict().items() if k != "value"})


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None  # IOCs leave unset limits at NaN, which isn't valid JSON
    return value


def _field_pair(result: Any, low: str, high: str) -> tuple[float, float] | None:
    """A (low, high) limit pair, or None when unset: IOCs leave limits at NaN or 0/0."""
    lower, upper = _get_field(result, low), _get_field(result, high)
    if lower is None or upper is None or math.isnan(lower) or math.isnan(upper) or lower == upper == 0:
        return None
    return (lower, upper)
