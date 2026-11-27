import json

import numpy as np
import pytest

from argus.mcp_server.server import build_registry
from argus.providers.epics.models import PVValue


async def _call(ctx, args):
    return json.loads((await build_registry().dispatch("get_pv", args, ctx))[0].text)


@pytest.fixture
def pvs(app_context, monkeypatch):
    live = {
        "CAM:A:ArrayRate_RBV": PVValue("CAM:A:ArrayRate_RBV", 3.0000001, severity="NO_ALARM"),
        "CAM:B:ArrayRate_RBV": PVValue("CAM:B:ArrayRate_RBV", 2.0, severity="MAJOR"),
        "CAM:A:Image": PVValue("CAM:A:Image", np.arange(1000)),
    }
    calls = {"reads": [], "patterns": []}

    async def get_pvs(names, timeout=5.0):
        calls["reads"].append(list(names))
        return [live.get(n, PVValue(n, None, status="disconnected")) for n in names]

    async def pv_names_matching(pattern, limit):
        calls["patterns"].append(pattern)
        import fnmatch

        names = sorted(n for n in live if fnmatch.fnmatchcase(n, pattern))
        return names[:limit], len(names) > limit

    monkeypatch.setattr(app_context.operations_service, "get_pvs", get_pvs)
    monkeypatch.setattr(app_context.operations_service, "pv_names_matching", pv_names_matching)
    return calls


async def test_list_is_read_in_one_batch_with_alarms_and_missing(app_context, pvs):
    out = await _call(app_context, {"pv_names": ["CAM:A:ArrayRate_RBV", "CAM:B:ArrayRate_RBV", "CAM:X:Gone",
                                                  "CAM:A:ArrayRate_RBV"]})
    assert out["status"] == "success" and out["count"] == 3
    assert pvs["reads"] == [["CAM:A:ArrayRate_RBV", "CAM:B:ArrayRate_RBV", "CAM:X:Gone"]]  # one call, deduped
    assert out["values"] == {"CAM:A:ArrayRate_RBV": 3.0, "CAM:B:ArrayRate_RBV": 2.0}
    assert out["alarms"] == {"CAM:B:ArrayRate_RBV": "MAJOR"}
    assert out["not_answering"] == ["CAM:X:Gone"]


async def test_pattern_reads_every_match(app_context, pvs):
    out = await _call(app_context, {"pattern": "CAM:*:ArrayRate_RBV"})
    assert pvs["patterns"] == ["CAM:*:ArrayRate_RBV"]
    assert set(out["values"]) == {"CAM:A:ArrayRate_RBV", "CAM:B:ArrayRate_RBV"}


async def test_long_arrays_are_summarised(app_context, pvs):
    out = await _call(app_context, {"pv_names": ["CAM:A:Image"]})
    assert out["values"]["CAM:A:Image"] == {"array_length": 1000, "first": list(range(16))}


async def test_pattern_without_matches_says_so(app_context, pvs):
    out = await _call(app_context, {"pattern": "NOPE:*"})
    assert out["count"] == 0 and "No PV" in out["note"]


@pytest.mark.parametrize("args", [
    {"pv_names": []}, {"pv_names": "CAM:A"}, {"pv_names": ["x"] * 501}, {"pattern": "*"},
    {"pattern": "CAM:*", "pv_names": ["CAM:A"]}, {"pv_name": "CAM:A", "pattern": "CAM:*"},
])
async def test_validation(app_context, pvs, args):
    out = await _call(app_context, args)
    assert out["code"] == "validation_error", args


async def test_single_pv_answer_is_unchanged(app_context, monkeypatch):
    async def get_pv(name):
        return PVValue(name, 4.2, severity="NO_ALARM")

    monkeypatch.setattr(app_context.operations_service, "get_pv", get_pv)
    out = await _call(app_context, {"pv_name": "QF12:CURRENT"})
    assert out["value"] == 4.2 and "values" not in out


def test_star_stays_within_one_level_double_star_crosses():
    from argus.services.operations_service import _level_pattern

    one = _level_pattern("EUAPS:CAM:*:ArrayRate_RBV")
    assert one.fullmatch("EUAPS:CAM:FI1-CAM-01:ArrayRate_RBV")
    assert not one.fullmatch("EUAPS:CAM:FI1-CAM-01:Overlay1:ArrayRate_RBV")
    deep = _level_pattern("EUAPS:CAM:FI1-CAM-01:**:ArrayRate_RBV")
    assert deep.fullmatch("EUAPS:CAM:FI1-CAM-01:Proc1:Stats1:ArrayRate_RBV")
    assert _level_pattern("A:B?").fullmatch("A:B1") and not _level_pattern("A:B?").fullmatch("A:B:")
    assert _level_pattern("A.B:*").fullmatch("A.B:x") and not _level_pattern("A.B:*").fullmatch("AxB:x")
