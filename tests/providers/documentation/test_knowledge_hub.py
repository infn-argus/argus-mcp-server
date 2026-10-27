import pytest
import respx

from argus.config.settings import KnowledgeHubSettings
from argus.providers.documentation.exceptions import DocumentationUnconfiguredError
from argus.providers.documentation.knowledge_hub import KnowledgeHubDocumentationProvider
from argus.providers.knowledge_hub.client import KnowledgeHubClient
from tests.providers.knowledge_hub.fake_hub import URL, FakeHub, hub_client


async def test_unconfigured_raises_clean_error():
    provider = KnowledgeHubDocumentationProvider(KnowledgeHubClient(KnowledgeHubSettings(_env_file=None)))
    assert provider.is_configured() is False
    with pytest.raises(DocumentationUnconfiguredError):
        await provider.search("bake-out")


def _passages(args, ws):
    return {
        "available": True,
        "results": [
            {
                "kind": "document",
                "title": f"Bake-out ({ws})",
                "where": "§2",
                "excerpt": f"Heat the {ws} chamber slowly.",
                "record": {"uid": f"d-{ws}", "code": f"PROC-{ws}", "link": f"[PROC-{ws} — Bake-out](https://hub/{ws})"},
                "score": 0.03 if ws == "btf" else 0.05,
            }
        ],
    }


@respx.mock
async def test_passages_from_every_workspace_are_merged_by_score_and_cited_by_link():
    hub = FakeHub({"search_knowledge": _passages}, workspaces=["btf", "controls-catalogue"])
    respx.post(URL).mock(side_effect=hub)
    provider = KnowledgeHubDocumentationProvider(hub_client())

    results = await provider.search("bake-out", top_k=5)

    assert [r.source_path for r in results] == [
        "[PROC-controls-catalogue — Bake-out](https://hub/controls-catalogue) (§2)",
        "[PROC-btf — Bake-out](https://hub/btf) (§2)",
    ]
    assert results[0].excerpt == "Heat the controls-catalogue chamber slowly."
    assert {c["workspace"] for c in hub.tool_calls("search_knowledge")} == {"btf", "controls-catalogue"}


@respx.mock
async def test_a_workspace_without_an_embedding_index_falls_back_to_text_search():
    hub = FakeHub(
        {
            "search_knowledge": lambda args, ws: {"available": False, "reason": "No embedding model"},
            "search_documents": lambda args, ws: {
                "total": 2,
                "documents": [
                    {"uid": "d2", "code": "RB-7", "title": "Vacuum runbook", "matched": "text", "excerpt": "...bake-out..."},
                    {"uid": "d1", "code": "PROC-1", "title": "Bake-out", "matched": "title"},
                ],
            },
        }
    )
    respx.post(URL).mock(side_effect=hub)
    results = await KnowledgeHubDocumentationProvider(hub_client()).search("bake-out", top_k=5)
    assert [r.source_path for r in results] == ["PROC-1 — Bake-out", "RB-7 — Vacuum runbook"]
    assert results[1].excerpt == "...bake-out..."
