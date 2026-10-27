"""Electronic logbook provider, against an olog-compatible REST API.

Olog/phoebus-olog is the de facto standard EPICS-world electronic logbook
(used at SNS, ESS, BNL, etc.); this client is generic enough to also work
against olog-API-compatible forks.

Authentication: when the tool call carries the caller's own access token
(core/request_context.current_caller_token - forwarded by LibreChat from the
user's INFN AAI login), Olog is called as that person, Bearer token - Olog
validates it as an OAuth2 resource server (its oauth2.issueUri), so reads
follow their permissions and entries they create are theirs. Otherwise the
configured account (LOGBOOK_USERNAME/LOGBOOK_PASSWORD), if any, is used.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import ClassVar

import httpx

from argus.config.settings import LogbookSettings
from argus.core.request_context import current_caller_token
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.logbook.exceptions import (
    LogbookQueryError,
    LogbookTimeoutError,
    LogbookUnavailableError,
    LogbookUnconfiguredError,
)
from argus.providers.logbook.models import LogbookEntry


class LogbookProvider:
    name: ClassVar[str] = "logbook"

    def __init__(self, settings: LogbookSettings) -> None:
        self._settings = settings

    def is_configured(self) -> bool:
        return bool(self._settings.base_url)

    def _auth(self) -> tuple[str, str] | None:
        if self._settings.username and self._settings.password:
            return (self._settings.username, self._settings.password)
        return None

    def _client(self, timeout: float) -> httpx.AsyncClient:
        """As the caller when this tool call carries their token, else as the configured account."""
        token = current_caller_token()
        if token:
            return httpx.AsyncClient(timeout=timeout, headers={"Authorization": f"Bearer {token}"})
        return httpx.AsyncClient(timeout=timeout, auth=self._auth())

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            async with httpx.AsyncClient(timeout=3, auth=self._auth()) as client:
                resp = await client.get(f"{self._settings.base_url}/Olog/logbooks")
            if resp.status_code < 500:
                return ProviderHealth(name=self.name, status=ProviderStatus.OK)
        except httpx.HTTPError as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))
        return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE)

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise LogbookUnconfiguredError("Logbook is not configured (set LOGBOOK_BASE_URL).")

    async def search_entries(
        self,
        *,
        logbook: str | None = None,
        tags: list[str] | None = None,
        text: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        timeout: float = 5.0,
    ) -> list[LogbookEntry]:
        self._require_configured()
        params: dict[str, str] = {"limit": str(limit)}
        if logbook:
            params["logbooks"] = logbook
        if tags:
            params["tags"] = ",".join(tags)
        if text:
            # "text" (also accepted as "desc"/"description") is the real
            # olog service's free-text-in-description search param -- "search"
            # is not recognized at all and was silently ignored server-side
            # (LogSearchUtil.java's param switch has an explicit "unsupported
            # search parameters are ignored" default case), confirmed by
            # reading phoebus-olog's actual source. This means every existing
            # caller passing `text` (get_alarm_history, get_maintenance_history)
            # was never actually filtering by it.
            params["text"] = text
        if since:
            # phoebus-olog parses start/end with its own TimestampFormats
            # (phoebus-olog/src/main/java/org/phoebus/util/time/), which only
            # knows space-separated patterns - "yyyy-MM-dd HH:mm:ss.SSS" and
            # shorter - and no ISO form at all: "2026-10-20T15:10:00Z" is
            # rejected with 400 "Invalid start and end times", confirmed live
            # against BTF's Olog (the copy in phoebus/core/util does accept
            # ISO_INSTANT, which is what an earlier fix was based on). Those
            # patterns carry no zone: Olog reads them in the `tz` parameter's
            # zone, else the server's own. So send UTC and say so.
            params["start"] = _olog_timestamp(since)
            params["tz"] = "UTC"

        url = f"{self._settings.base_url}/Olog/logs"
        try:
            async with self._client(timeout) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise LogbookTimeoutError("Timeout searching logbook entries") from exc
        except httpx.HTTPError as exc:
            raise LogbookUnavailableError(f"Logbook unreachable: {exc}") from exc

        try:
            return [_to_entry(item) for item in payload]
        except (KeyError, TypeError) as exc:
            raise LogbookQueryError(f"Unexpected logbook response shape: {exc}") from exc

    async def create_entry(
        self, title: str, text: str, logbooks: list[str], tags: list[str] | None = None, *, timeout: float = 5.0
    ) -> LogbookEntry:
        self._require_configured()
        body = {
            "title": title,
            "description": text,
            "logbooks": [{"name": lb} for lb in logbooks],
            "tags": [{"name": t} for t in (tags or [])],
        }
        url = f"{self._settings.base_url}/Olog/logs"
        try:
            async with self._client(timeout) as client:
                resp = await client.put(url, json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise LogbookTimeoutError("Timeout creating logbook entry") from exc
        except httpx.HTTPError as exc:
            raise LogbookUnavailableError(f"Logbook unreachable: {exc}") from exc

        return _to_entry(payload)

    async def _get_raw(self, client: httpx.AsyncClient, olog_id: str) -> dict:
        resp = await client.get(f"{self._settings.base_url}/Olog/logs/{olog_id}")
        if resp.status_code == 404:
            raise LogbookQueryError(f"No logbook entry {olog_id}.")
        resp.raise_for_status()
        return resp.json()

    async def get_entry(self, entry_id: str | int, *, timeout: float = 5.0) -> LogbookEntry:
        """One entry, read straight from Olog - e.g. before changing it."""
        self._require_configured()
        olog_id = olog_entry_id(entry_id)
        try:
            async with self._client(timeout) as client:
                payload = await self._get_raw(client, olog_id)
        except httpx.TimeoutException as exc:
            raise LogbookTimeoutError(f"Timeout reading logbook entry {olog_id}") from exc
        except httpx.HTTPError as exc:
            raise LogbookUnavailableError(f"Logbook unreachable: {exc}") from exc
        return _to_entry(payload)

    async def update_entry(
        self,
        entry_id: str | int,
        *,
        title: str | None = None,
        text: str | None = None,
        logbooks: list[str] | None = None,
        tags: list[str] | None = None,
        timeout: float = 5.0,
    ) -> LogbookEntry:
        """Change an existing entry. Olog keeps the previous version as history.

        Olog's update replaces the whole record (POST /Olog/logs/{id}), so the
        current one is read first and only the given fields change - its
        attachments, properties and anything else ARGUS doesn't model are sent
        back untouched. Olog checks that the caller may write to every logbook
        involved, so with a forwarded user token this is that person's edit.
        """
        self._require_configured()
        olog_id = olog_entry_id(entry_id)
        try:
            async with self._client(timeout) as client:
                record = await self._get_raw(client, olog_id)
                if title is not None:
                    record["title"] = title
                if text is not None:
                    record["description"] = text
                    if "source" in record:
                        record["source"] = text
                if logbooks is not None:
                    record["logbooks"] = [{"name": lb} for lb in logbooks]
                if tags is not None:
                    record["tags"] = [{"name": t} for t in tags]
                resp = await client.post(f"{self._settings.base_url}/Olog/logs/{olog_id}", json=record)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.TimeoutException as exc:
            raise LogbookTimeoutError(f"Timeout updating logbook entry {olog_id}") from exc
        except httpx.HTTPError as exc:
            raise LogbookUnavailableError(f"Logbook refused or unreachable updating entry {olog_id}: {exc}") from exc
        return _to_entry(payload)


# A bare Olog id, or the Knowledge Hub's code for that entry: OLOG-<FACILITY>-<id>,
# with a workspace suffix when two workspaces share a facility (olog_logbook.py).
# Strict on purpose: a device name like "QF12" must not become entry 12.
_OLOG_ID = re.compile(r"^(?:OLOG-[A-Za-z0-9_]+-)?(\d+)(?:-[A-Za-z0-9-]+)?$", re.IGNORECASE)


def olog_entry_id(entry_id: str | int) -> str:
    """Olog's numeric id from "1234", 1234, or the Knowledge Hub's code for that entry ("OLOG-SPARC-1234")."""
    text = str(entry_id).strip()
    match = _OLOG_ID.match(text)
    if not text or match is None or (not text.isdigit() and not text.upper().startswith("OLOG-")):
        raise LogbookQueryError(f"Not a logbook entry id: {entry_id!r} (expected e.g. 1234 or OLOG-SPARC-1234).")
    return match.group(1)


def _olog_timestamp(when: datetime) -> str:
    """`when` in UTC, as Olog's "yyyy-MM-dd HH:mm:ss.SSS"; a naive datetime is taken as UTC."""
    utc = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return utc.strftime("%Y-%m-%d %H:%M:%S.") + f"{utc.microsecond // 1000:03d}"


def _to_entry(payload: dict) -> LogbookEntry:
    created = payload.get("createdDate")
    return LogbookEntry(
        id=str(payload.get("id", "")),
        title=payload.get("title", ""),
        text=payload.get("description", ""),
        logbooks=[lb.get("name") for lb in payload.get("logbooks", [])],
        tags=[t.get("name") for t in payload.get("tags", [])],
        created_at=datetime.fromtimestamp(created / 1000) if isinstance(created, (int, float)) else None,
        author=payload.get("owner"),
    )
