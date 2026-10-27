from datetime import datetime, timezone

import httpx

import pytest
import respx

from argus.config.settings import LogbookSettings
from argus.providers.logbook.exceptions import LogbookUnconfiguredError
from argus.providers.logbook.knowledge_hub import KnowledgeHubLogbookProvider
from argus.providers.logbook.logbook import LogbookProvider
from tests.providers.knowledge_hub.fake_hub import URL, FakeHub, hub_client

_ENTRIES = {
    "OLOG-BTF-101": (
        "QUATM002 tripped",
        "**Logged:** 2026-10-20 08:15 UTC · **By:** jdoe · **Logbooks:** Operations · **Tags:** alarm, magnets\n\n"
        "QUATM002 tripped on overtemperature.\n\n[Open in Olog](https://olog/101)",
    ),
    "OLOG-BTF-99": (
        "QUATM002 maintenance",
        "**Logged:** 2026-09-01 10:00 UTC · **By:** asmith · **Logbooks:** Maintenance · **Tags:** maintenance\n\n"
        "Replaced the QUATM002 fan.",
    ),
    "OLOG-BTF-150": ("Shift notes", "Free text with no header, mentions QUATM002."),
}


def _search(args, ws):
    assert args["type"] == "Logbook Entry"
    needle = args["query"].lower()
    return {
        "total": 3,
        "documents": [
            {"uid": code.lower(), "code": code, "title": title, "type": "Logbook Entry"}
            for code, (title, body) in _ENTRIES.items()
            if needle in body.lower() or needle in title.lower()
        ],
    }


def _get(args, ws):
    code = args["uid_or_code"].upper()
    title, body = _ENTRIES[code]
    return {
        "found": True, "uid": code.lower(), "code": code, "title": title, "body_markdown": body,
        "link": f"[{code} — {title}](https://hub/{code})",
    }


@respx.mock
async def test_entries_are_parsed_from_the_olog_header_newest_first():
    hub = FakeHub({"search_documents": _search, "get_document": _get})
    respx.post(URL).mock(side_effect=hub)
    entries = await KnowledgeHubLogbookProvider(hub_client()).search_entries(text="QUATM002")

    assert [e.id for e in entries] == ["OLOG-BTF-101", "OLOG-BTF-99", "OLOG-BTF-150"]
    first = entries[0]
    assert first.created_at == datetime(2026, 10, 20, 8, 15, tzinfo=timezone.utc)
    assert first.author == "jdoe"
    assert first.logbooks == ["Operations"]
    assert first.tags == ["alarm", "magnets"]
    assert first.text.startswith("QUATM002 tripped on overtemperature.")
    assert first.url == "[OLOG-BTF-101 — QUATM002 tripped](https://hub/OLOG-BTF-101)"
    # No header: the whole body is the text, and nothing is guessed.
    assert entries[2].created_at is None and entries[2].text.startswith("Free text")


@respx.mock
async def test_tags_and_since_filter_and_a_tag_alone_drives_the_text_search():
    hub = FakeHub({"search_documents": _search, "get_document": _get})
    respx.post(URL).mock(side_effect=hub)
    provider = KnowledgeHubLogbookProvider(hub_client())

    alarms = await provider.search_entries(tags=["alarm"], since=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [e.id for e in alarms] == ["OLOG-BTF-101"]
    assert hub.tool_calls("search_documents")[0]["arguments"]["query"] == "alarm"

    # A naive `since` is taken as UTC; an entry with no date never passes a since filter.
    recent = await provider.search_entries(text="QUATM002", since=datetime(2026, 8, 1))
    assert [e.id for e in recent] == ["OLOG-BTF-101", "OLOG-BTF-99"]

    maintenance = await provider.search_entries(text="QUATM002", logbook="maintenance")
    assert [e.id for e in maintenance] == ["OLOG-BTF-99"]


async def test_posting_is_delegated_to_olog_or_refused_clearly():
    provider = KnowledgeHubLogbookProvider(hub_client(), writer=LogbookProvider(LogbookSettings(_env_file=None)))
    with pytest.raises(LogbookUnconfiguredError, match="read-only"):
        await provider.create_entry("t", "x", ["Operations"])



@respx.mock
async def test_reading_one_entry_to_change_it_goes_to_olog_and_needs_it():
    respx.get("http://olog.test/Olog/logs/1234").mock(
        return_value=httpx.Response(200, json={"id": 1234, "title": "QF12", "description": "x"})
    )
    with_olog = KnowledgeHubLogbookProvider(
        hub_client(), writer=LogbookProvider(LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test"))
    )
    assert (await with_olog.get_entry("OLOG-BTF-1234")).title == "QF12"

    without_olog = KnowledgeHubLogbookProvider(hub_client(), writer=LogbookProvider(LogbookSettings(_env_file=None)))
    with pytest.raises(LogbookUnconfiguredError, match="needs Olog"):
        await without_olog.update_entry("OLOG-BTF-1234", text="y")
