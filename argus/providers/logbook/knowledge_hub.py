"""Logbook reads from the ARGUS Knowledge Hub.

The hub keeps each facility's Olog as Logbook Entry documents (its daily
upload job, argus-knowledge-hub's services/olog_logbook.py), with the
entry's who/when/where written as the first paragraph of the body:

    **Logged:** 2026-10-01 12:30 UTC · **By:** jdoe · **Logbooks:** Operations · **Tags:** alarm, vacuum

Neither of the hub's document tools returns those as fields, so search
here is search_documents (type "Logbook Entry", by text) followed by
get_document on the best candidates and a parse of that header -- which is
what makes `since`/`tags`/`logbook` filtering possible at all.

Candidates are fetched newest first by Olog id (the trailing number of a
code like OLOG-BTF-12345, monotonic in Olog), capped at
_MAX_FETCHED_PER_WORKSPACE get_document calls per workspace. A count over a
long window (machine_summary's 24 h alarm count) is therefore a lower bound
on a busy logbook, never an overcount.

The hub is read-only: create_entry is delegated to the Olog provider when
one is configured, so create_logbook_entry keeps working.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any, ClassVar

from argus.providers.base import ProviderHealth
from argus.providers.knowledge_hub.client import KnowledgeHubClient
from argus.providers.knowledge_hub.exceptions import (
    KnowledgeHubError,
    KnowledgeHubTimeoutError,
    KnowledgeHubUnconfiguredError,
)
from argus.providers.logbook.exceptions import (
    LogbookQueryError,
    LogbookTimeoutError,
    LogbookUnconfiguredError,
)
from argus.providers.logbook.logbook import LogbookProvider
from argus.providers.logbook.models import LogbookEntry

LOGBOOK_DOCUMENT_TYPE = "Logbook Entry"
_MAX_FETCHED_PER_WORKSPACE = 60
_CONCURRENCY = 8

_HEADER_FIELD = re.compile(r"\*\*(Logged|By|Logbooks|Tags|Level):\*\*\s*(.*?)\s*(?=·\s*\*\*|$)")
_CODE_NUMBER = re.compile(r"(\d+)(?:-[A-Z0-9-]+)?$")


class KnowledgeHubLogbookProvider:
    name: ClassVar[str] = "knowledge_hub_logbook"

    def __init__(self, client: KnowledgeHubClient, writer: LogbookProvider | None = None) -> None:
        self._client = client
        self._writer = writer

    def is_configured(self) -> bool:
        return self._client.is_configured()

    async def health(self) -> ProviderHealth:
        health = await self._client.health()
        return ProviderHealth(name=self.name, status=health.status, detail=health.detail)

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
        # With no text, the first tag narrows the hub's text search: the
        # header carries "**Tags:** alarm", so every entry tagged alarm
        # matches "alarm" (plus some that merely mention it, filtered below).
        query = text or (tags[0] if tags else "")
        try:
            workspaces = await self._client.workspace_ids()
            results, _errors = await self._client.for_each_workspace(
                workspaces, lambda ws: self._search_workspace(ws, query, limit, timeout)
            )
        except KnowledgeHubUnconfiguredError as exc:
            raise LogbookUnconfiguredError(str(exc)) from exc
        except KnowledgeHubTimeoutError as exc:
            raise LogbookTimeoutError(f"Timeout searching Knowledge Hub logbook entries: {exc}") from exc
        except KnowledgeHubError as exc:
            raise LogbookQueryError(f"Knowledge Hub logbook search failed: {exc}") from exc

        entries = [e for ws in workspaces for e in results.get(ws, [])]
        entries = [e for e in entries if _matches(e, logbook=logbook, tags=tags, since=since)]
        entries.sort(key=lambda e: e.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return entries[:limit]

    async def _search_workspace(self, workspace: str, query: str, limit: int, timeout: float) -> list[LogbookEntry]:
        found = await self._client.call_tool(
            "search_documents",
            {"query": query, "type": LOGBOOK_DOCUMENT_TYPE, "limit": 100},
            workspace=workspace,
            timeout=timeout,
        )
        documents = [d for d in (found or {}).get("documents") or [] if isinstance(d, dict) and d.get("uid")]
        documents.sort(key=_olog_number, reverse=True)
        wanted = documents[: min(_MAX_FETCHED_PER_WORKSPACE, max(limit * 3, 20))]

        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def fetch(summary: dict[str, Any]) -> LogbookEntry | None:
            async with semaphore:
                document = await self._client.call_tool(
                    "get_document", {"uid_or_code": summary["uid"]}, workspace=workspace, timeout=timeout
                )
            if not isinstance(document, dict) or not document.get("found"):
                return None
            return _to_entry({**summary, **document})

        fetched = await asyncio.gather(*(fetch(d) for d in wanted))
        return [e for e in fetched if e is not None]

    async def create_entry(
        self, title: str, text: str, logbooks: list[str], tags: list[str] | None = None, *, timeout: float = 5.0
    ) -> LogbookEntry:
        if self._writer is None or not self._writer.is_configured():
            raise LogbookUnconfiguredError(
                "The Knowledge Hub is read-only; posting a logbook entry needs Olog (set LOGBOOK_BASE_URL)."
            )
        return await self._writer.create_entry(title, text, logbooks, tags=tags, timeout=timeout)


def _olog_number(document: dict[str, Any]) -> int:
    match = _CODE_NUMBER.search(str(document.get("code") or ""))
    return int(match.group(1)) if match else -1


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_logged(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _to_entry(document: dict[str, Any]) -> LogbookEntry:
    body = document.get("body_markdown") or ""
    head, _, rest = body.partition("\n\n")
    fields = dict(_HEADER_FIELD.findall(head)) if "**" in head else {}
    text = rest if fields else body
    return LogbookEntry(
        id=str(document.get("code") or document.get("uid") or ""),
        title=document.get("title") or "",
        text=text,
        logbooks=_split(fields.get("Logbooks", "")),
        tags=_split(fields.get("Tags", "")),
        created_at=_parse_logged(fields["Logged"]) if "Logged" in fields else None,
        author=fields.get("By") or None,
        url=document.get("link") or document.get("url"),
    )


def _matches(entry: LogbookEntry, *, logbook: str | None, tags: list[str] | None, since: datetime | None) -> bool:
    if logbook and logbook.lower() not in {lb.lower() for lb in entry.logbooks}:
        return False
    if tags and not {t.lower() for t in tags} & {t.lower() for t in entry.tags}:
        return False
    if since is not None:
        if entry.created_at is None:
            return False
        reference = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        if entry.created_at < reference:
            return False
    return True
