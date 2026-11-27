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
    assert route.calls.last.request.url.params["pv"] == "optimized_120(GUNQUA01:CURRENT_RB)"
    assert payload["status"] == "success"
    assert (payload["mode"], payload["units"], payload["point_count"], payload["samples_in_range"]) == (
        "binned", "A", 2, 462)
    assert payload["columns"] == ["time", "mean", "min", "max", "count"]
    # 6 significant digits, UTC ISO times
    assert payload["points"][0] == ["2026-11-26T12:40:48Z", -0.00207418, -0.00256, -0.00162, 232]
    assert payload["summary"] == {"min": -0.00256, "max": -0.00162, "first": -0.00207418, "last": -0.00201}
    assert payload["viewer_url"] == (
        "http://archiver.test/retrieval/ui/viewer/archViewer.html?pv=GUNQUA01:CURRENT_RB"
        "&from=2026-11-26T12:00:00.000Z&to=2026-11-27T12:00:00.000Z"
    )


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


# --- compare_with and plot_pv_history ---------------------------------------


def _raw(points):
    return [{"secs": s, "nanos": 0, "val": v, "severity": 0} for s, v in points]


def _archiver_by_pv(series_by_pv, units="A"):
    def handler(request):
        pv = request.url.params["pv"]
        name = pv[pv.index("(") + 1:-1] if "(" in pv else pv
        return httpx.Response(200, json=[{"meta": {"EGU": units}, "data": series_by_pv.get(name, [])}])
    return handler


@respx.mock
async def test_get_history_compare_with_returns_each_series_and_one_viewer(app_context):
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({
        "A": _raw([(1795696848, 1.0), (1795696900, 2.0)]), "B": _raw([(1795696848, 5.0)]),
    }))
    payload = await _history(app_context, {
        "pv_name": "A", "compare_with": ["B", "A"],
        "start": "2026-11-26T12:00:00+00:00", "end": "2026-11-27T12:00:00+00:00",
    })
    assert [s["pv_name"] for s in payload["series"]] == ["A", "B"]
    assert payload["series"][1]["summary"]["last"] == 5.0
    assert "pv=A&pv=B&from=" in payload["viewer_url"]


async def _plot(app_context, args):
    app_context.archiver._settings.base_url = "http://archiver.test"
    return await build_registry().dispatch("plot_pv_history", args, app_context)


_T0 = 1795680000  # 2026-11-26T08:00:00Z
_RANGE = {"start": "2026-11-26T08:00:00+00:00", "end": "2026-11-26T09:00:00+00:00"}


@respx.mock
async def test_plot_trend_returns_numbers_and_an_inline_chart_page(app_context):
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({"A": _raw([(_T0 + i * 60, float(i)) for i in range(60)])}))
    response = await _plot(app_context, {"pvs": ["A"], **_RANGE})
    payload = json.loads(response[0].text)
    assert payload["status"] == "success" and payload["kind"] == "trend"
    assert payload["stats"]["A"]["max"] == 59.0 and payload["pvs"][0]["units"] == "A"
    assert "_attachments" not in payload and "Don't redraw" in payload["chart"]
    page = response[1].resource
    assert str(page.uri).startswith("ui://argus/plot/") and page.mimeType == "text/html"
    assert "Plotly.newPlot" in page.text and "ui-size-change" in page.text


@respx.mock
async def test_plot_scatter_correlation_and_fit(app_context):
    a = [(_T0 + i * 10, float(i)) for i in range(300)]
    b = [(_T0 + i * 10, 2.0 * i + 1.0) for i in range(300)]
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({"A": _raw(a), "B": _raw(b)}))
    payload = json.loads((await _plot(app_context, {"pvs": ["A", "B"], "kind": "scatter", **_RANGE}))[0].text)
    assert payload["pearson_r"] > 0.999
    assert abs(payload["fit"]["slope"] - 2.0) < 0.01 and abs(payload["fit"]["intercept"] - 1.0) < 0.1


@respx.mock
async def test_plot_difference_holds_last_value_between_samples(app_context):
    # A steps 0 -> 10 at +30 min; B is constant 4: A-B is -4 then 6.
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({
        "A": _raw([(_T0, 0.0), (_T0 + 1800, 10.0)]), "B": _raw([(_T0, 4.0)]),
    }))
    payload = json.loads((await _plot(app_context, {"pvs": ["A", "B"], "kind": "difference", **_RANGE}))[0].text)
    d = payload["difference"]
    assert (d["min"], d["max"], d["max_abs"]) == (-4.0, 6.0, 6.0)


@respx.mock
async def test_plot_fft_finds_the_period(app_context):
    import math as m

    # 10-minute oscillation sampled every 5 s over an hour.
    pts = [(_T0 + i * 5, m.sin(2 * m.pi * i * 5 / 600)) for i in range(720)]
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({"A": _raw(pts)}))
    payload = json.loads((await _plot(app_context, {"pvs": ["A"], "kind": "fft", **_RANGE}))[0].text)
    top = max(payload["spectra"]["A"]["peaks"], key=lambda p: p["amplitude"])
    assert abs(top["period_s"] - 600) < 40


@respx.mock
async def test_plot_histogram_percentiles(app_context):
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({"A": _raw([(_T0 + i, float(i % 100)) for i in range(1000)])}))
    payload = json.loads((await _plot(app_context, {"pvs": ["A"], "kind": "histogram", **_RANGE}))[0].text)
    st = payload["stats"]["A"]
    assert st["p50"] == 49.5 and st["min"] == 0.0 and st["max"] == 99.0


async def test_plot_validation(app_context):
    for args in ({"pvs": []}, {"pvs": ["A"], "kind": "pie"}, {"pvs": ["A"], "kind": "scatter"},
                 {"pvs": ["A", "B", "C"], "kind": "difference"}, {"pvs": ["A"] * 7},
                 {"pvs": ["A"], "bins": 1}):
        payload = json.loads((await _plot(app_context, args))[0].text)
        assert payload["code"] == "validation_error", args


@respx.mock
async def test_plot_unarchived_pv_is_a_clear_error(app_context):
    respx.get(_ARCHIVER).mock(side_effect=_archiver_by_pv({}))
    payload = json.loads((await _plot(app_context, {"pvs": ["NOPE"], **_RANGE}))[0].text)
    assert payload["code"] == "validation_error" and "NOPE" in payload["message"]
