"""Loki provider — LogQL queries over the platform's shared Loki instance.

Structured filters only (namespace/pod/container/free-text), never raw LogQL,
from the tool layer down: this provider builds the query string itself so the
LLM-facing tool schema can't smuggle in extra label matchers or pipeline
stages through an unescaped filter value.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

import httpx

from argus.config.settings import LokiSettings
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.loki.exceptions import (
    LokiQueryError,
    LokiTimeoutError,
    LokiUnavailableError,
    LokiUnconfiguredError,
)
from argus.providers.loki.models import LokiLogEntry


class LokiProvider:
    name: ClassVar[str] = "loki"

    def __init__(self, settings: LokiSettings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.base_url)

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self._settings.base_url}/ready")
            if resp.status_code == 200:
                return ProviderHealth(name=self.name, status=ProviderStatus.OK)
            return ProviderHealth(
                name=self.name, status=ProviderStatus.UNAVAILABLE, detail=f"status {resp.status_code}"
            )
        except httpx.HTTPError as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise LokiUnconfiguredError("Loki is not configured (set LOKI_BASE_URL).")

    async def search(
        self,
        *,
        namespace: str | None = None,
        pod: str | None = None,
        container: str | None = None,
        query: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
        timeout: float = 8.0,
    ) -> list[LokiLogEntry]:
        self._require_configured()
        since = since or (datetime.now(timezone.utc) - timedelta(hours=1))
        end = datetime.now(timezone.utc)
        limit = limit or self._settings.default_limit

        params = {
            "query": _build_query(namespace=namespace, pod=pod, container=container, text=query),
            "limit": str(limit),
            "start": str(_to_unix_nanos(since)),
            "end": str(_to_unix_nanos(end)),
            "direction": "backward",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self._settings.base_url}/loki/api/v1/query_range", params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise LokiTimeoutError(f"Timeout querying Loki: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LokiUnavailableError(f"Loki unreachable: {exc}") from exc

        try:
            streams = payload["data"]["result"]
        except (KeyError, TypeError) as exc:
            raise LokiQueryError(f"Unexpected Loki response shape: {exc}") from exc

        entries = [_to_entry(stream, value) for stream in streams for value in stream.get("values", [])]
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]


    async def metric(self, logql: str, *, timeout: float = 30.0) -> list[tuple[dict[str, str], float]]:
        """An instant LogQL metric query (count_over_time...), as (labels, value) pairs."""
        self._require_configured()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self._settings.base_url}/loki/api/v1/query", params={"query": logql})
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise LokiTimeoutError(f"Timeout querying Loki: {exc}") from exc
        except httpx.HTTPError as exc:
            raise LokiUnavailableError(f"Loki unreachable: {exc}") from exc
        try:
            return [(r.get("metric", {}), float(r["value"][1])) for r in payload["data"]["result"]]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise LokiQueryError(f"Unexpected Loki response shape: {exc}") from exc


def _build_query(*, namespace: str | None, pod: str | None, container: str | None, text: str | None) -> str:
    # json.dumps (not an f-string) quotes each label/text value LogQL-safely
    # (Loki's string literal syntax matches Go/JSON escaping closely enough).
    # These values can come straight from tool arguments, so naive
    # interpolation would let a crafted value break out of its matcher/filter
    # and inject extra label selectors or pipeline stages.
    matchers: list[str] = []
    if namespace:
        matchers.append(f"namespace={json.dumps(namespace)}")
    if pod:
        matchers.append(f"pod={json.dumps(pod)}")
    if container:
        matchers.append(f"container={json.dumps(container)}")
    if not matchers:
        # Loki requires at least one label matcher; ".+" matches every
        # namespace so "no filters given" still means "search everything".
        matchers.append('namespace=~".+"')
    selector = "{" + ", ".join(matchers) + "}"
    if text:
        return f"{selector} |= {json.dumps(text)}"
    return selector


def _to_unix_nanos(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def _to_entry(stream: dict[str, Any], value: list[str]) -> LokiLogEntry:
    labels = stream.get("stream", {})
    ts_ns, line = value
    return LokiLogEntry(
        timestamp=datetime.fromtimestamp(int(ts_ns) / 1_000_000_000, tz=timezone.utc),
        namespace=labels.get("namespace", ""),
        pod=labels.get("pod", ""),
        container=labels.get("container", ""),
        line=line,
    )
