"""Archiver Appliance provider.

Talks to the well-known REST retrieval API
(``/retrieval/data/getData.json``) exposed by EPICS Archiver Appliance
deployments. No official Python client exists, so this is a thin httpx
wrapper — the same approach every EPICS site's own scripts use.
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import httpx

from argus.config.settings import ArchiverSettings
from argus.providers.archiver.exceptions import (
    ArchiverQueryError,
    ArchiverTimeoutError,
    ArchiverUnavailableError,
    ArchiverUnconfiguredError,
)
from argus.providers.archiver.models import ArchiverSample, ArchiverSeries
from argus.providers.base import ProviderHealth, ProviderStatus


class ArchiverApplianceProvider:
    name: ClassVar[str] = "archiver"

    def __init__(self, settings: ArchiverSettings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.base_url)

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._settings.base_url}/retrieval/bpl/getVersion")
            if resp.status_code < 500:
                return ProviderHealth(name=self.name, status=ProviderStatus.OK)
        except httpx.HTTPError as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))
        return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise ArchiverUnconfiguredError("Archiver Appliance is not configured (set ARCHIVER_BASE_URL).")

    async def get_data(
        self, pv_name: str, start: datetime, end: datetime, *, operator: str | None = None, timeout: float = 8.0
    ) -> ArchiverSeries:
        self._require_configured()
        pv_query = f"{operator}({pv_name})" if operator else pv_name
        params = {"pv": pv_query, "from": _iso(start), "to": _iso(end)}
        url = f"{self._settings.base_url}/retrieval/data/getData.json"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise ArchiverTimeoutError(f"Timeout retrieving archiver data for '{pv_name}'") from exc
        except httpx.HTTPError as exc:
            raise ArchiverUnavailableError(f"Archiver Appliance unreachable: {exc}") from exc

        try:
            series = payload[0]
            samples = [
                ArchiverSample(
                    timestamp=datetime.fromtimestamp(point["secs"] + point.get("nanos", 0) / 1e9),
                    value=point["val"],
                    severity=point.get("severity"),
                )
                for point in series.get("data", [])
            ]
        except (KeyError, IndexError, TypeError) as exc:
            raise ArchiverQueryError(f"Unexpected Archiver Appliance response shape for '{pv_name}': {exc}") from exc

        return ArchiverSeries(pv_name=pv_name, samples=samples)

    async def get_latest(self, pv_name: str, *, timeout: float = 5.0) -> ArchiverSample | None:
        now = datetime.utcnow()
        series = await self.get_data(pv_name, now.replace(hour=0, minute=0, second=0), now, timeout=timeout)
        return series.samples[-1] if series.samples else None


    # --- management: which PVs are archived, and archiving more ------------
    # The appliance's management BPL (/mgmt/bpl/...), on the same host as retrieval.

    async def pv_status(self, pv_names: list[str], *, timeout: float = 10.0) -> dict[str, str]:
        """Each PV's archiving status as the appliance words it ("Being archived",
        "Not being archived", "Initial sampling"...)."""
        self._require_configured()
        status: dict[str, str] = {}
        for chunk in _chunks(pv_names, 50):
            payload = await self._mgmt("GET", "getPVStatus", params={"pv": ",".join(chunk)}, timeout=timeout)
            for row in payload if isinstance(payload, list) else []:
                if isinstance(row, dict) and row.get("pvName"):
                    status[row["pvName"]] = str(row.get("status") or "unknown")
        return status

    async def archive_pvs(self, pv_names: list[str], *, timeout: float = 30.0) -> dict[str, str]:
        """Ask the appliance to archive these PVs (its defaults decide the sampling).
        Returns each PV's answer, e.g. "Archive request submitted"."""
        self._require_configured()
        answers: dict[str, str] = {}
        for chunk in _chunks(pv_names, 50):
            payload = await self._mgmt("POST", "archivePV", json=[{"pv": pv} for pv in chunk], timeout=timeout)
            for row in payload if isinstance(payload, list) else []:
                if isinstance(row, dict) and row.get("pvName"):
                    answers[row["pvName"]] = str(row.get("status") or "unknown")
        return answers

    async def _mgmt(self, method: str, action: str, *, params: dict | None = None, json: object = None,
                    timeout: float) -> object:
        url = f"{self._settings.base_url}/mgmt/bpl/{action}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, params=params, json=json)
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as exc:
            raise ArchiverTimeoutError(f"Timeout calling the Archiver Appliance's {action}") from exc
        except httpx.HTTPStatusError as exc:
            raise ArchiverQueryError(
                f"The Archiver Appliance refused {action} (HTTP {exc.response.status_code}): {exc.response.text[:200]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ArchiverUnavailableError(f"Archiver Appliance {action} failed: {exc}") from exc


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")
