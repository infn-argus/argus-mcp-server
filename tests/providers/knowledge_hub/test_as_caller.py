"""KNOWLEDGE_HUB_AS_CALLER: the hub is asked as the person behind each tool call, with their own sign-in, so it
applies their access; never with the service token, and nothing one person was answered serves another."""

import pytest
import respx

from argus.config.settings import KnowledgeHubSettings
from argus.core.request_context import caller_token_scope
from argus.providers.base import ProviderStatus
from argus.providers.knowledge_hub.client import KnowledgeHubClient
from argus.providers.knowledge_hub.exceptions import KnowledgeHubAuthError
from tests.providers.knowledge_hub.fake_hub import URL, FakeHub, hub_client

ANNA, BRUNO = "aai-token-anna", "aai-token-bruno"


def per_person():
    """One hub answering each sign-in with that person's workspaces."""
    hubs = {
        f"Bearer {ANNA}": FakeHub({"search_objects": lambda a, ws: {"who": "anna", "ws": ws}}, ["btf", "sparc"]),
        f"Bearer {BRUNO}": FakeHub({"search_objects": lambda a, ws: {"who": "bruno", "ws": ws}}, ["btf"]),
    }

    def answer(request):
        return hubs[request.headers["authorization"]](request)

    return hubs, answer


def as_caller():
    return hub_client(KNOWLEDGE_HUB_AS_CALLER="true")


@pytest.mark.asyncio
@respx.mock
async def test_each_call_is_made_with_the_callers_own_sign_in():
    hubs, answer = per_person()
    respx.post(URL).mock(side_effect=answer)
    client = as_caller()
    with caller_token_scope(ANNA):
        assert await client.call_tool("search_objects") == {"who": "anna", "ws": "btf"}
        assert [w.id for w in await client.list_workspaces()] == ["btf", "sparc"]
    with caller_token_scope(BRUNO):
        assert await client.call_tool("search_objects") == {"who": "bruno", "ws": "btf"}
        # Not Anna's cached list: Bruno's own.
        assert [w.id for w in await client.list_workspaces()] == ["btf"]
    # Each person had their own session: one handshake each, and the service token never sent.
    for hub in hubs.values():
        assert [r["method"] for r in hub.requests].count("initialize") == 1
        assert all("argus_pat_test" not in r["headers"]["authorization"] for r in hub.requests)


@pytest.mark.asyncio
@respx.mock
async def test_a_call_with_no_sign_in_is_refused_not_answered_with_the_service_token():
    route = respx.post(URL)
    client = as_caller()
    with pytest.raises(KnowledgeHubAuthError, match="no sign-in"):
        await client.call_tool("search_objects")
    with pytest.raises(KnowledgeHubAuthError):
        await client.list_workspaces()
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_needs_no_service_token_and_health_does_not_report_it_down():
    client = KnowledgeHubClient(KnowledgeHubSettings(_env_file=None, KNOWLEDGE_HUB_URL=URL, KNOWLEDGE_HUB_AS_CALLER="true"))
    assert client.is_configured()
    assert (await client.health()).status == ProviderStatus.OK


@pytest.mark.asyncio
@respx.mock
async def test_without_the_setting_the_service_token_is_used_as_before():
    hub = FakeHub({"search_objects": lambda a, ws: {"ok": True}})
    respx.post(URL).mock(side_effect=hub)
    with caller_token_scope(ANNA):
        assert await hub_client().call_tool("search_objects") == {"ok": True}
    assert all(r["headers"]["authorization"] == "Bearer argus_pat_test" for r in hub.requests)
