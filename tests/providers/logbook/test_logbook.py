from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from argus.config.settings import LogbookSettings
from argus.core.request_context import caller_token_scope
from argus.providers.logbook.exceptions import LogbookUnavailableError, LogbookUnconfiguredError
from argus.providers.logbook.logbook import LogbookProvider


def _configured() -> LogbookProvider:
    return LogbookProvider(LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test"))


async def test_unconfigured_raises_clean_error():
    provider = LogbookProvider(LogbookSettings(_env_file=None))
    with pytest.raises(LogbookUnconfiguredError):
        await provider.search_entries(text="QF12")


@respx.mock
async def test_search_entries_parses_payload():
    provider = _configured()
    respx.get("http://olog.test/Olog/logs").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "title": "QF12 tripped",
                    "description": "Overcurrent alarm",
                    "logbooks": [{"name": "operations"}],
                    "tags": [{"name": "alarm"}],
                    "owner": "shift-crew",
                }
            ],
        )
    )
    entries = await provider.search_entries(text="QF12", tags=["alarm"])
    assert entries[0].title == "QF12 tripped"
    assert entries[0].logbooks == ["operations"]
    assert entries[0].tags == ["alarm"]


@respx.mock
async def test_search_entries_sends_text_param_not_search():
    # olog's real search param for free-text-in-description is "text" (also
    # accepted as "desc"/"description") -- "search" is not recognized and is
    # silently dropped server-side (confirmed against phoebus-olog's own
    # LogSearchUtil.java). Regression test for that exact bug.
    provider = _configured()
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))
    await provider.search_entries(text="QF12", tags=["alarm"])

    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params["text"] == "QF12"
    assert "search" not in sent_params


@respx.mock
async def test_search_entries_sends_since_in_olog_format_as_utc():
    # phoebus-olog's own TimestampFormats only parses space-separated
    # "yyyy-MM-dd HH:mm:ss.SSS"-style patterns - no ISO form at all, with "Z"
    # or "+00:00" - read in the `tz` parameter's zone (else the server's).
    # Confirmed live against BTF's Olog: "2026-10-20T15:10:00Z" -> 400,
    # "2026-10-20 15:10:00.000" + tz=UTC -> 200. Regression test for the 400.
    provider = _configured()
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))
    since = datetime(2026, 7, 15, 14, 56, 42, 900935, tzinfo=timezone.utc)
    await provider.search_entries(text="QF12", since=since)

    sent_params = dict(route.calls.last.request.url.params)
    assert sent_params["start"] == "2026-07-15 14:56:42.900"
    assert sent_params["tz"] == "UTC"


@respx.mock
async def test_search_entries_converts_since_to_utc_and_reads_naive_as_utc():
    provider = _configured()
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))
    rome = timezone(timedelta(hours=2))
    await provider.search_entries(since=datetime(2026, 7, 15, 16, 56, 42, tzinfo=rome))
    assert dict(route.calls.last.request.url.params)["start"] == "2026-07-15 14:56:42.000"

    await provider.search_entries(since=datetime(2026, 7, 15, 14, 56, 42))
    assert dict(route.calls.last.request.url.params)["start"] == "2026-07-15 14:56:42.000"


@respx.mock
async def test_search_entries_without_since_sends_no_time_window():
    provider = _configured()
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))
    await provider.search_entries(text="QF12")
    sent_params = dict(route.calls.last.request.url.params)
    assert "start" not in sent_params and "tz" not in sent_params


@respx.mock
async def test_unreachable_backend_raises_unavailable():
    provider = _configured()
    respx.get("http://olog.test/Olog/logs").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LogbookUnavailableError):
        await provider.search_entries(text="QF12")


async def test_create_entry_unconfigured_raises_clean_error():
    provider = LogbookProvider(LogbookSettings(_env_file=None))
    with pytest.raises(LogbookUnconfiguredError):
        await provider.create_entry("title", "text", ["operations"])


@respx.mock
async def test_create_entry_sends_expected_body_and_parses_response():
    provider = _configured()
    route = respx.put("http://olog.test/Olog/logs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 42,
                "title": "Shift handover",
                "description": "All nominal.",
                "logbooks": [{"name": "operations"}],
                "tags": [{"name": "maintenance"}],
                "owner": "shift-crew",
            },
        )
    )
    entry = await provider.create_entry(
        "Shift handover", "All nominal.", ["operations"], tags=["maintenance"]
    )
    assert entry.id == "42"
    assert entry.title == "Shift handover"
    assert entry.logbooks == ["operations"]
    assert entry.tags == ["maintenance"]

    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["title"] == "Shift handover"
    assert body["description"] == "All nominal."
    assert body["logbooks"] == [{"name": "operations"}]
    assert body["tags"] == [{"name": "maintenance"}]


@respx.mock
async def test_create_entry_unreachable_raises_unavailable():
    provider = _configured()
    respx.put("http://olog.test/Olog/logs").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LogbookUnavailableError):
        await provider.create_entry("title", "text", ["operations"])


@respx.mock
async def test_with_a_caller_token_olog_is_called_as_that_person():
    provider = LogbookProvider(
        LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test", LOGBOOK_USERNAME="epics", LOGBOOK_PASSWORD="pw")
    )
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))
    with caller_token_scope("eyJuser"):
        await provider.search_entries(text="QF12")
    # The person's token, and not the configured account alongside it.
    assert route.calls.last.request.headers["authorization"] == "Bearer eyJuser"


@respx.mock
async def test_without_a_caller_token_the_configured_account_is_used():
    provider = LogbookProvider(
        LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test", LOGBOOK_USERNAME="epics", LOGBOOK_PASSWORD="pw")
    )
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))
    await provider.search_entries(text="QF12")
    assert route.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
async def test_an_entry_created_with_a_caller_token_is_created_as_that_person():
    route = respx.put("http://olog.test/Olog/logs").mock(
        return_value=httpx.Response(200, json={"id": 7, "title": "t", "description": "x", "owner": "jdoe"})
    )
    with caller_token_scope("eyJuser"):
        entry = await _configured().create_entry("t", "x", ["Operations"])
    assert route.calls.last.request.headers["authorization"] == "Bearer eyJuser"
    assert entry.author == "jdoe"
