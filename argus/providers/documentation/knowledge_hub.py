"""ARGUS Knowledge Hub-backed DocumentationProvider.

Same DocumentationProvider protocol as the local TF-IDF and RAGFLOW
providers, so search_documentation and diagnose_device work unchanged with
DOCUMENTATION_BACKEND=knowledge_hub.

Per workspace, asks the hub's search_knowledge (passages of procedures,
tickets, comments and attached manuals, by meaning and by words). A
workspace whose AI endpoint has no embedding model answers that tool with
{"available": false}; there it falls back to search_documents, the hub's
title/code/body text match, rather than returning nothing.

source_path is the hub's ready-made Markdown link to the record whenever
the hub gives one, so an answer citing it links to the record's page in
the hub instead of naming an opaque uid.
"""

from __future__ import annotations

from typing import Any, ClassVar

from argus.providers.base import ProviderHealth
from argus.providers.documentation.exceptions import (
    DocumentationQueryError,
    DocumentationUnavailableError,
    DocumentationUnconfiguredError,
)
from argus.providers.documentation.models import DocSearchResult
from argus.providers.knowledge_hub.client import KnowledgeHubClient
from argus.providers.knowledge_hub.exceptions import (
    KnowledgeHubError,
    KnowledgeHubUnconfiguredError,
)

# search_documents has no relevance score; title matches are what the hub
# itself ranks first, so they rank above body-text matches here too.
_TITLE_MATCH_SCORE = 1.0
_TEXT_MATCH_SCORE = 0.5


class KnowledgeHubDocumentationProvider:
    name: ClassVar[str] = "knowledge_hub_documentation"

    def __init__(self, client: KnowledgeHubClient) -> None:
        self._client = client

    def is_configured(self) -> bool:
        return self._client.is_configured()

    async def health(self) -> ProviderHealth:
        health = await self._client.health()
        return ProviderHealth(name=self.name, status=health.status, detail=health.detail)

    def reindex(self) -> None:
        # The hub keeps its own knowledge index (its AI settings); nothing for ARGUS to do.
        pass

    async def search(self, query: str, top_k: int = 5, *, timeout: float | None = None) -> list[DocSearchResult]:
        try:
            workspaces = await self._client.workspace_ids()
            results, _errors = await self._client.for_each_workspace(
                workspaces, lambda ws: self._search_workspace(ws, query, top_k, timeout)
            )
        except KnowledgeHubUnconfiguredError as exc:
            raise DocumentationUnconfiguredError(str(exc)) from exc
        except KnowledgeHubError as exc:
            raise DocumentationUnavailableError(f"Knowledge Hub documentation search failed: {exc}") from exc

        merged = [r for ws in workspaces for r in results.get(ws, [])]
        merged.sort(key=lambda r: r.score, reverse=True)
        return merged[:top_k]

    async def _search_workspace(
        self, workspace: str, query: str, top_k: int, timeout: float | None
    ) -> list[DocSearchResult]:
        payload = await self._client.call_tool(
            "search_knowledge", {"query": query, "limit": min(top_k, 20)}, workspace=workspace, timeout=timeout
        )
        if not isinstance(payload, dict):
            raise DocumentationQueryError(f"Unexpected search_knowledge response: {payload!r}"[:300])
        if payload.get("available") is False:
            return await self._search_documents(workspace, query, top_k, timeout)
        return [_passage(item) for item in payload.get("results") or [] if isinstance(item, dict)]

    async def _search_documents(
        self, workspace: str, query: str, top_k: int, timeout: float | None
    ) -> list[DocSearchResult]:
        payload = await self._client.call_tool(
            "search_documents", {"query": query, "limit": top_k}, workspace=workspace, timeout=timeout
        )
        if not isinstance(payload, dict):
            raise DocumentationQueryError(f"Unexpected search_documents response: {payload!r}"[:300])
        return [_document(item) for item in payload.get("documents") or [] if isinstance(item, dict)]


def _cite(record: dict[str, Any], fallback: str) -> str:
    return record.get("link") or record.get("url") or fallback


def _passage(item: dict[str, Any]) -> DocSearchResult:
    record = item.get("record") if isinstance(item.get("record"), dict) else {}
    title = item.get("title") or record.get("title") or record.get("code") or "untitled"
    where = f" ({item['where']})" if item.get("where") else ""
    return DocSearchResult(
        source_path=_cite(record, title) + where,
        excerpt=(item.get("excerpt") or "")[:400],
        # A workspace with a re-ranker orders passages by rerank_score; its
        # hybrid `score` is only what chose which passages the re-ranker saw.
        score=float(item.get("rerank_score") or item.get("score") or 0.0),
    )


def _document(item: dict[str, Any]) -> DocSearchResult:
    title = " — ".join(p for p in (item.get("code"), item.get("title")) if p) or "untitled"
    return DocSearchResult(
        source_path=_cite(item, title),
        excerpt=(item.get("excerpt") or item.get("title") or "")[:400],
        score=_TITLE_MATCH_SCORE if item.get("matched") == "title" else _TEXT_MATCH_SCORE,
    )
