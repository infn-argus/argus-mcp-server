"""Historical data: archiver trends, alarm/maintenance history, log search.

Alarm and maintenance history are modeled as Logbook entries filtered by tag
convention (tag=alarm, tag=maintenance) rather than inventing a dedicated
provider — the spec's provider list has none, and Logbook already fits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.archiver.models import ArchiverSeries
from argus.providers.elastic.elasticsearch import ElasticsearchProvider
from argus.providers.elastic.models import LogRecord
from argus.providers.logbook.logbook import LogbookProvider
from argus.providers.logbook.models import LogbookEntry
from argus.providers.loki.loki import LokiProvider
from argus.providers.loki.models import LokiLogEntry


class HistoryService:
    def __init__(
        self,
        archiver: ArchiverApplianceProvider,
        logbook: LogbookProvider,
        elastic: ElasticsearchProvider,
        loki: LokiProvider,
    ) -> None:
        self._archiver = archiver
        self._logbook = logbook
        self._elastic = elastic
        self._loki = loki

    async def get_history(self, pv_name: str, start: datetime, end: datetime) -> ArchiverSeries:
        return await self._archiver.get_data(pv_name, start, end)

    async def get_alarm_history(self, device_name: str, since: datetime | None = None) -> list[LogbookEntry]:
        since = since or datetime.now(timezone.utc) - timedelta(days=7)
        return await self._logbook.search_entries(text=device_name, tags=["alarm"], since=since)

    async def get_maintenance_history(self, device_name: str, since: datetime | None = None) -> list[LogbookEntry]:
        since = since or datetime.now(timezone.utc) - timedelta(days=30)
        return await self._logbook.search_entries(text=device_name, tags=["maintenance"], since=since)

    async def get_logs(
        self, query: str, *, index: str | None = None, since: datetime | None = None, limit: int = 100
    ) -> list[LogRecord]:
        return await self._elastic.search_logs(query=query, index=index, since=since, limit=limit)

    async def search_pod_logs(
        self,
        *,
        namespace: str | None = None,
        pod: str | None = None,
        container: str | None = None,
        query: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[LokiLogEntry]:
        return await self._loki.search(
            namespace=namespace, pod=pod, container=container, query=query, since=since, limit=limit
        )

    async def create_logbook_entry(
        self, title: str, text: str, logbooks: list[str], tags: list[str] | None = None
    ) -> LogbookEntry:
        return await self._logbook.create_entry(title, text, logbooks, tags=tags)

    async def get_logbook_entry(self, entry_id: str | int) -> LogbookEntry:
        return await self._logbook.get_entry(entry_id)

    async def update_logbook_entry(
        self,
        entry_id: str | int,
        *,
        title: str | None = None,
        text: str | None = None,
        logbooks: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> LogbookEntry:
        return await self._logbook.update_entry(entry_id, title=title, text=text, logbooks=logbooks, tags=tags)
