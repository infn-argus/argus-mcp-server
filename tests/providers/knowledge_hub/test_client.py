import httpx
import pytest
import respx

from argus.config.settings import KnowledgeHubSettings
from argus.providers.base import ProviderStatus
from argus.providers.knowledge_hub.client import KnowledgeHubClient
from argus.providers.knowledge_hub.exceptions import (
    KnowledgeHubAuthError,
    KnowledgeHubQueryError,
    KnowledgeHubUnavailableError,
    KnowledgeHubUnconfiguredError,
)
from tests.providers.knowledge_hub.fake_hub import URL, FakeHub, ToolFailure, hub_client


async def test_unconfigured_raises_clean_error():
    client = KnowledgeHubClient(KnowledgeHubSettings(_env_file=None))
    assert client.is_configured() is False
    assert (await client.health()).status == ProviderStatus.UNCONFIGURED
    with pytest.raises(KnowledgeHubUnconfiguredError):
        await client.call_tool("search_objects")


@respx.mock
async def test_handshake_happens_once_and_the_token_is_sent():
    hub = FakeHub({"search_objects": lambda args, ws: {"total": 0, "objects": []}})
    respx.post(URL).mock(side_effect=hub)
    client = hub_client()

    await client.call_tool("search_objects", {"query": "QUA"})
    await client.call_tool("search_objects", {"query": "DIP"})

    methods = [r["method"] for r in hub.requests]
    assert methods == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    assert hub.requests[0]["headers"]["authorization"] == "Bearer argus_pat_test"


@respx.mock
async def test_a_session_id_is_echoed_and_an_expired_session_reinitializes():
    hub = FakeHub({"ping_tool": lambda args, ws: {"ok": True}}, session_id="s-1")
    route = respx.post(URL)
    route.mock(side_effect=hub)
    client = hub_client()

    assert await client.call_tool("ping_tool") == {"ok": True}
    assert hub.requests[-1]["headers"]["mcp-session-id"] == "s-1"

    # The server forgets the session: 404 once, then it answers again.
    forgot = {"done": False}

    def forget_once(request: httpx.Request) -> httpx.Response:
        if not forgot["done"] and request.headers.get("mcp-session-id"):
            forgot["done"] = True
            return httpx.Response(404)
        return hub(request)

    route.mock(side_effect=forget_once)
    hub.session_id = "s-2"
    assert await client.call_tool("ping_tool") == {"ok": True}
    assert [r["method"] for r in hub.requests[-3:]] == ["initialize", "notifications/initialized", "tools/call"]
    assert hub.requests[-1]["headers"]["mcp-session-id"] == "s-2"


@respx.mock
async def test_an_sse_answer_is_read_like_a_json_one():
    hub = FakeHub({"search_objects": lambda args, ws: {"total": 1}}, sse=True)
    respx.post(URL).mock(side_effect=hub)
    assert await hub_client().call_tool("search_objects") == {"total": 1}


@respx.mock
async def test_a_failing_tool_is_a_query_error_naming_the_workspace():
    def broken(args, ws):
        raise ToolFailure("KeyError: boom")

    respx.post(URL).mock(side_effect=FakeHub({"search_objects": broken}))
    with pytest.raises(KnowledgeHubQueryError, match="search_objects in workspace 'btf' failed: KeyError: boom"):
        await hub_client().call_tool("search_objects", workspace="btf")


@respx.mock
async def test_a_refused_token_is_an_auth_error():
    respx.post(URL).mock(return_value=httpx.Response(401, json={"detail": "Invalid or revoked token"}))
    with pytest.raises(KnowledgeHubAuthError, match="Invalid or revoked token"):
        await hub_client().call_tool("search_objects")


@respx.mock
async def test_unreachable_hub_is_unavailable_and_health_says_so():
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    client = hub_client()
    with pytest.raises(KnowledgeHubUnavailableError):
        await client.call_tool("search_objects")
    assert (await client.health()).status == ProviderStatus.UNAVAILABLE


@respx.mock
async def test_blank_workspaces_means_every_one_the_token_may_read():
    # An administrator's admin-scoped token: list_workspaces answers with all of them.
    hub = FakeHub(workspaces=["btf", "sparc", "controls-catalogue"])
    respx.post(URL).mock(side_effect=hub)
    client = hub_client()
    assert await client.workspace_ids() == ["btf", "sparc", "controls-catalogue"]
    # No header is pinned when no workspace is configured.
    assert "x-workspace-id" not in hub.tool_calls("list_workspaces")[0]["headers"]


@respx.mock
async def test_configured_workspaces_narrow_and_drop_unreadable_ones():
    hub = FakeHub(workspaces=["btf", "sparc"])
    respx.post(URL).mock(side_effect=hub)
    client = hub_client("sparc, nope")
    assert await client.workspace_ids() == ["sparc"]
    assert hub.tool_calls("list_workspaces")[0]["headers"]["x-workspace-id"] == "sparc"
    with pytest.raises(KnowledgeHubQueryError, match="not one this server asks in"):
        await client.workspace_ids(only="btf")


@respx.mock
async def test_no_readable_workspace_is_a_clear_error():
    respx.post(URL).mock(side_effect=FakeHub(workspaces=["btf"]))
    with pytest.raises(KnowledgeHubQueryError, match="none of KNOWLEDGE_HUB_WORKSPACES"):
        await hub_client("eli").workspace_ids()


async def test_fan_out_degrades_on_one_workspace_and_raises_when_all_fail():
    client = hub_client()

    async def work(ws: str) -> str:
        if ws == "bad":
            raise KnowledgeHubQueryError("nope")
        return ws.upper()

    results, errors = await client.for_each_workspace(["btf", "bad"], work)
    assert results == {"btf": "BTF"} and errors == {"bad": "nope"}
    with pytest.raises(KnowledgeHubQueryError):
        await client.for_each_workspace(["bad"], work)
