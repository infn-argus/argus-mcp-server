import json

import httpx
import respx

from argus.mcp_server.server import build_registry


async def test_create_logbook_entry_missing_title_returns_validation_error(app_context):
    registry = build_registry()
    response = await registry.dispatch(
        "create_logbook_entry", {"text": "body", "logbooks": ["operations"]}, app_context
    )
    payload = json.loads(response[0].text)
    assert payload["status"] == "error"
    assert payload["code"] == "validation_error"


async def test_create_logbook_entry_missing_logbooks_returns_validation_error(app_context):
    registry = build_registry()
    response = await registry.dispatch(
        "create_logbook_entry", {"title": "t", "text": "body"}, app_context
    )
    payload = json.loads(response[0].text)
    assert payload["status"] == "error"
    assert payload["code"] == "validation_error"


async def test_create_logbook_entry_unconfigured_returns_structured_error(app_context):
    registry = build_registry()
    response = await registry.dispatch(
        "create_logbook_entry",
        {"title": "t", "text": "body", "logbooks": ["operations"]},
        app_context,
    )
    payload = json.loads(response[0].text)
    assert payload["status"] == "error"
    assert payload["code"] == "logbook_unconfigured"


@respx.mock
async def test_create_logbook_entry_success(app_context):
    app_context.logbook._settings.base_url = "http://olog.test"
    respx.put("http://olog.test/Olog/logs").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1,
                "title": "t",
                "description": "body",
                "logbooks": [{"name": "operations"}],
                "tags": [],
                "owner": "shift-crew",
            },
        )
    )
    registry = build_registry()
    response = await registry.dispatch(
        "create_logbook_entry",
        {"title": "t", "text": "body", "logbooks": ["operations"]},
        app_context,
    )
    payload = json.loads(response[0].text)
    assert payload["status"] == "success"
    assert payload["title"] == "t"
    assert payload["logbooks"] == ["operations"]


# --- get_history: down-sampled, chart-ready ---------------------------------

_ARCHIVER = "http://archiver.test/retrieval/data/getData.json"


def _bin(secs, mean, low, high, count):
    return {"secs": secs, "nanos": 0, "val": [mean, 0.1, low, high, float(count)], "severity": 0, "status": 0}


async def _history(app_context, args):
    app_context.archiver._settings.base_url = "http://archiver.test"
    response = await build_registry().dispatch("get_history", args, app_context)
    return json.loads(response[0].text)


@respx.mock
async def test_get_history_asks_the_archiver_to_downsample_and_returns_bins(app_context):
    route = respx.get(_ARCHIVER).mock(
        return_value=httpx.Response(200, json=[{
            "meta": {"name": "GUNQUA01:CURRENT_RB", "EGU": "A", "PREC": "5"},
            "data": [_bin(1795696848, -0.0020741810344827587, -0.00256, -0.00162, 232),
                     _bin(1795697136, -0.00201, -0.0021, -0.0019, 230)],
        }])
    )
    payload = await _history(app_context, {
        "pv_name": "GUNQUA01:CURRENT_RB", "start": "2026-11-26T12:00:00+00:00", "end": "2026-11-27T12:00:00+00:00",
    })
    assert route.calls.last.request.url.params["pv"] == "optimized_300(GUNQUA01:CURRENT_RB)"
    assert payload["status"] == "success"
    assert (payload["mode"], payload["units"], payload["point_count"], payload["samples_in_range"]) == (
        "binned", "A", 2, 462)
    assert payload["columns"] == ["time", "mean", "min", "max", "count"]
    # 6 significant digits, UTC ISO times
    assert payload["points"][0] == ["2026-11-26T12:40:48Z", -0.00207418, -0.00256, -0.00162, 232]
    assert payload["summary"] == {"min": -0.00256, "max": -0.00162, "first": -0.00207418, "last": -0.00201}


@respx.mock
async def test_get_history_short_range_comes_back_raw(app_context):
    respx.get(_ARCHIVER).mock(
        return_value=httpx.Response(200, json=[{
            "meta": {"EGU": "A"},
            "data": [{"secs": 1795696848, "nanos": 0, "val": 0.0002, "severity": 0},
                     {"secs": 1795696849, "nanos": 0, "val": 0.0005, "severity": 1}],
        }])
    )
    payload = await _history(app_context, {"pv_name": "AC1HCR01:CURRENT_RB", "max_points": 50})
    assert payload["mode"] == "raw" and payload["columns"] == ["time", "value"]
    assert payload["points"] == [["2026-11-26T12:40:48Z", 0.0002], ["2026-11-26T12:40:49Z", 0.0005]]
    assert payload["alarm_points"] == [["2026-11-26T12:40:49Z", 1]]
    assert payload["summary"]["max"] == 0.0005


@respx.mock
async def test_get_history_empty_range_says_so(app_context):
    respx.get(_ARCHIVER).mock(return_value=httpx.Response(200, json=[{"data": []}]))
    payload = await _history(app_context, {"pv_name": "NOT:ARCHIVED"})
    assert payload["mode"] == "empty" and payload["points"] == [] and "not be archived" in payload["note"]


async def test_get_history_validates_max_points_and_range(app_context):
    for args in ({"pv_name": "X", "max_points": 5}, {"pv_name": "X", "max_points": 5000},
                 {"pv_name": "X", "max_points": "300"},
                 {"pv_name": "X", "start": "2026-11-27T12:00:00", "end": "2026-11-27T11:00:00"}):
        payload = await _history(app_context, args)
        assert payload["code"] == "validation_error", args
