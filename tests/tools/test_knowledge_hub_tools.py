"""The same server, built from the legacy YAML/RAG settings or from the ARGUS Knowledge Hub."""

import json

import pytest
import respx

from argus.config.settings import Settings
from argus.core.context import AppContext
from argus.mcp_server.server import build_registry
from argus.providers.documentation.knowledge_hub import KnowledgeHubDocumentationProvider
from argus.providers.documentation.rag import LocalTfidfDocumentationProvider
from argus.providers.logbook.knowledge_hub import KnowledgeHubLogbookProvider
from argus.providers.logbook.logbook import LogbookProvider
from argus.services.beamline_inventory_service import BeamlineInventoryService
from argus.services.knowledge_hub_inventory_service import KnowledgeHubInventoryService
from tests.providers.knowledge_hub.fake_hub import URL, FakeHub

_HUB_ENV = {
    "KNOWLEDGE_HUB_URL": URL,
    "KNOWLEDGE_HUB_TOKEN": "argus_pat_test",
    "INVENTORY_BACKEND": "knowledge_hub",
    "DOCUMENTATION_BACKEND": "knowledge_hub",
    "LOGBOOK_BACKEND": "knowledge_hub",
}


@pytest.fixture
def hub_env(monkeypatch):
    # Nested provider settings (KnowledgeHubSettings) read the environment
    # themselves, exactly as in a deployment -- not Settings(...) kwargs.
    for key, value in _HUB_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def _hub_settings() -> Settings:
    return Settings(_env_file=None)


def test_legacy_settings_build_the_legacy_backends():
    ctx = AppContext.build(Settings(_env_file=None))
    assert isinstance(ctx.beamline_inventory_service, BeamlineInventoryService)
    assert isinstance(ctx.documentation, LocalTfidfDocumentationProvider)
    assert isinstance(ctx.logbook, LogbookProvider)
    assert ctx.knowledge_hub is not None and ctx.knowledge_hub.is_configured() is False


def test_hub_settings_build_the_hub_backends_on_one_client(hub_env):
    ctx = AppContext.build(_hub_settings())
    assert isinstance(ctx.beamline_inventory_service, KnowledgeHubInventoryService)
    assert isinstance(ctx.documentation, KnowledgeHubDocumentationProvider)
    assert isinstance(ctx.logbook, KnowledgeHubLogbookProvider)
    assert ctx.knowledge_hub.is_configured()
    # diagnose_device and get_alarm_history read the same hub logbook.
    assert ctx.history_service._logbook is ctx.logbook
    assert ctx.diagnostics_service._logbook is ctx.logbook


def test_the_registry_exposes_the_same_tool_names_plus_the_workspace_listing(monkeypatch):
    legacy = {t.name: t for t in build_registry(Settings(_env_file=None)).list_tools()}
    for key, value in _HUB_ENV.items():
        monkeypatch.setenv(key, value)
    hub = {t.name: t for t in build_registry(_hub_settings()).list_tools()}

    assert set(hub) - set(legacy) == {"list_knowledge_hub_workspaces"}
    assert "repo" in legacy["list_beamline_devices"].inputSchema["properties"]
    assert "workspace" in hub["list_beamline_devices"].inputSchema["properties"]
    assert "repo" not in hub["list_beamline_devices"].inputSchema["properties"]
    assert "Knowledge Hub" in hub["list_beamline_devices"].description
    # No settings at all keeps the legacy tool set (what existing tests and clients see).
    assert {t.name for t in build_registry().list_tools()} == set(legacy)


@respx.mock
async def test_list_beamline_devices_answers_from_the_hub_with_links_and_workspaces(hub_env):
    hub = FakeHub(
        {
            "search_objects": lambda args, ws: {
                "total": 1,
                "objects": [
                    {
                        "uid": "u1",
                        "name": "QUATM002",
                        "type": "Control Device",
                        "link": "[QUATM002](https://hub/assets/u1)",
                        "attributes": {"system": "mag", "pv_prefix": "BTF:MAG:DANFYSIK:QUATM002", "zones": ["TL"]},
                    }
                ],
            }
        },
        workspaces=["btf", "controls-catalogue"],
    )
    respx.post(URL).mock(side_effect=hub)
    hub_env.setenv("KNOWLEDGE_HUB_WORKSPACES", "btf")
    settings = _hub_settings()
    ctx = AppContext.build(settings)

    payload = json.loads((await build_registry(settings).dispatch("list_beamline_devices", {"devfunc": "QUA"}, ctx))[0].text)

    assert payload["status"] == "success" and payload["source"] == "knowledge_hub"
    assert payload["device_count"] == 1
    device = payload["devices"][0]
    assert device["workspace"] == "btf" and device["link"] == "[QUATM002](https://hub/assets/u1)"
    assert "notes" not in payload

    listed = json.loads((await build_registry(settings).dispatch("list_knowledge_hub_workspaces", {}, ctx))[0].text)
    assert listed["workspaces"] == [{"id": "btf", "name": "BTF", "shared": False}]
    assert listed["restricted_to"] == ["btf"]
