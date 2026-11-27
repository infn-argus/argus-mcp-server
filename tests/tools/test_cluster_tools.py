import json

import pytest

from argus.mcp_server.server import build_registry
from argus.services.cluster_service import ClusterReport, Finding


async def _call(ctx, name, args):
    response = await build_registry().dispatch(name, args, ctx)
    return json.loads(response[0].text)


@pytest.mark.parametrize(
    "args",
    [{}, {"pod": "a-0", "ioc": "a"}, {"pod": "a-0", "window_minutes": "5"}],
)
async def test_inspect_pod_argument_validation(app_context, args):
    payload = await _call(app_context, "inspect_pod", args)
    assert payload["status"] == "error" and payload["code"] == "validation_error"


async def test_inspect_node_requires_node(app_context):
    payload = await _call(app_context, "inspect_node", {})
    assert payload["code"] == "validation_error"


async def test_inspect_pod_by_ioc_unconfigured_kubernetes_is_structured_error(app_context, monkeypatch):
    app_context.kubernetes._configured = False
    payload = await _call(app_context, "inspect_pod", {"ioc": "sbncamera-03", "namespace": "sparc"})
    assert payload["status"] == "error" and payload["code"] == "kubernetes_unconfigured"


async def test_inspect_node_with_nothing_configured_degrades_not_fails(app_context):
    app_context.kubernetes._configured = False
    payload = await _call(app_context, "inspect_node", {"node": "plk8sdadom003"})
    assert payload["status"] == "success"
    assert payload["sources"]["node"].startswith("unconfigured")
    assert payload["sources"]["network"].startswith("skipped")
    assert payload["findings"] == []


async def test_inspect_node_passes_clamped_window_and_returns_findings(app_context, monkeypatch):
    seen = {}

    async def fake_inspect_node(node, *, window_minutes):
        seen.update(node=node, window=window_minutes)
        report = ClusterReport(subject=f"node {node}", window_minutes=window_minutes)
        report.findings.append(Finding("warning", "prometheus", "vlan110 drops"))
        return report

    monkeypatch.setattr(app_context.cluster_service, "inspect_node", fake_inspect_node)
    payload = await _call(app_context, "inspect_node", {"node": "plk8sdadom003", "window_minutes": 999})
    assert seen == {"node": "plk8sdadom003", "window": 60}
    assert payload["findings"] == [{"severity": "warning", "source": "prometheus", "message": "vlan110 drops"}]


async def test_cluster_tools_are_read_only(app_context):
    tools = {t.name: t for t in build_registry().list_tools(confirm_writes=True)}
    for name in ("inspect_pod", "inspect_node"):
        assert "confirmation_id" not in tools[name].inputSchema["properties"]


@pytest.mark.parametrize(
    "args",
    [{}, {"ioc": "a", "hosts": "192.168.0.5"}, {"ioc": "a", "port": 70000}, {"ioc": "a", "count": "3"}, {"hosts": ["192.168.0.5"], "vantage": "ioc"}],
)
async def test_check_connectivity_argument_validation(app_context, args):
    payload = await _call(app_context, "check_connectivity", args)
    assert payload["status"] == "error" and payload["code"] == "validation_error"


async def test_check_connectivity_refuses_public_hosts(app_context):
    payload = await _call(app_context, "check_connectivity", {"hosts": ["8.8.8.8"]})
    assert payload["status"] == "error" and "private" in payload["message"]
