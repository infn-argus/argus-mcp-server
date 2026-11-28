from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argus.providers.archiver.models import ArchiverSample, ArchiverSeries
from argus.services.daily_report import DailyReport, day_window, render, summarize_continuous, summarize_discrete
from argus.services.report_service import inventory, resolve

START = datetime(2026, 11, 27, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def _s(hours: float, value, **kw) -> ArchiverSample:
    return ArchiverSample(timestamp=START + timedelta(hours=hours), value=value, **kw)


def test_discrete_takes_the_starting_value_from_before_the_window():
    d = summarize_discrete([_s(-30, 1), _s(6, 0), _s(6, 0), _s(18, 1)], START, END)
    assert d.first == 1 and d.last == 1
    assert d.changes == [("2026-11-27T06:00:00Z", 0), ("2026-11-27T18:00:00Z", 1)]
    assert d.share == {"1": 0.5, "0": 0.5}


def test_discrete_without_earlier_sample_starts_at_the_first_one():
    d = summarize_discrete([_s(12, 2.0), _s(18, 3)], START, END)
    assert d.first == 2.0 and d.changes == [("2026-11-27T18:00:00Z", 3)]
    assert d.share == {"2": 0.25, "3": 0.25}       # nothing known before noon


def test_continuous_uses_bin_min_and_max():
    c = summarize_continuous([_s(0, 1.0, min=0.5, max=1.5, count=10), _s(12, 3.0, min=2.0, max=9.0, count=10)])
    assert (c.bins, c.first, c.last, c.min, c.max, c.mean) == (2, 1.0, 3.0, 0.5, 9.0, 2.0)


VALUES = """
epicsConfiguration:
  iocs:
    ps: {devgroup: mag, devtype: easydriver, iocprefix: X:MAG, devices: [{name: GUNSOL01}]}
    mot: {devgroup: mot, devtype: pollux, iocprefix: X:MOT, devices: [{name: FI3-HMN-01}]}
    cam: {devgroup: cam, devtype: camera, iocprefix: X:CAM, devices: [{name: CAM01}]}
    vac: {devgroup: vac, devtype: tpg, iocprefix: X:VAC, devices: [{name: VGA01}]}
"""
CONFIG = {
    "beamline": "X",
    "interfaces": {
        "magnet": {"readback": "{pv}:CURRENT_RB", "setpoint": "{pv}:CURRENT_SP", "state": "{pv}:STATE_RB"},
        "motor": {"readback": "{pv}.RBV", "done_moving": "{pv}.DMOV", "high_limit": "{pv}.HLS"},
        "camera": {"acquiring": "{pv}:Acquire_RBV", "frame_rate": "{pv}:ArrayRate_RBV",
                   "total": "{pv}:Stats1:Total_RBV", "centroid_x": "{pv}:Stats1:CentroidX_RBV"},
        "gauge": {"pressure": "{pv}:PRES_RB"},
    },
    "naming": {"pattern": r"^(?P<section>[A-Z0-9]{3})(?P<type>[A-Z]{3})"},
    "groups": {
        "magnets": {"select": {"devgroup": "mag"}, "interface": "magnet", "function_from": "type",
                    "functions": {"SOL": "solenoid"},
                    "checks": {"readback_vs_setpoint": {"abs": 0.05, "rel": 0.005}}},
        "motors": {"select": {"devgroup": "mot"}, "interface": "motor"},
        "cameras": {"select": {"devgroup": "cam"}, "interface": "camera"},
        "pointing": {"group_by": "zone", "checks": {"laser_present": {"above_fraction_of_median": 0.2}}},
        "vacuum": {"select": {"devgroup": "vac"}, "interface": "gauge",
                   "checks": {"spikes": {"factor_over_baseline": 10}}},
    },
    "trips": [{"name": "interlock", "watch": ["X:INTLK:*"]}],
}

HISTORY = {
    "X:MAG:GUNSOL01:CURRENT_SP": [_s(-48, 100.0), _s(9, 110.0), _s(10, 110.3)],
    "X:MAG:GUNSOL01:CURRENT_RB": [_s(h, 109.0) for h in range(0, 24, 6)],     # 1.3 A off the last setpoint
    "X:MAG:GUNSOL01:STATE_RB": [_s(-48, 1), _s(15, 3), _s(15.5, 1)],          # ON, fault, ON
    "X:MOT:FI3-HMN-01.DMOV": [_s(-5, 1), _s(8, 0), _s(8.1, 1), _s(20, 0), _s(20.2, 1)],
    "X:MOT:FI3-HMN-01.RBV": [_s(0, 11.0), _s(23, 12.5)],
    "X:MOT:FI3-HMN-01.HLS": [_s(-5, 1)],                                   # resting on it: not a hit
    "X:CAM:CAM01:Acquire_RBV": [_s(-1, 0), _s(6, 1), _s(12, 0)],
    "X:CAM:CAM01:ArrayRate_RBV": [_s(h, 3.0) for h in range(6, 12)],
    "X:CAM:CAM01:Stats1:Total_RBV": [_s(h, 5e5) for h in range(6, 12)],
    "X:CAM:CAM01:Stats1:CentroidX_RBV": [_s(6, 300.0), _s(9, 320.0, min=299.0, max=330.0)],
    "X:VAC:VGA01:PRES_RB": [_s(h, 1e-9, min=1e-9, max=1e-9) for h in range(24)] + [_s(13, 2e-9, max=5e-7)],
    "X:INTLK:CHL01": [_s(-2, 0), _s(14, 1), _s(14.2, 0)],
}


class _Archiver:
    async def get_data(self, pv, start, end, *, max_points=None, timeout=8.0):
        if pv not in HISTORY:
            raise RuntimeError("no data")
        return ArchiverSeries(pv_name=pv, samples=[s for s in HISTORY[pv] if s.timestamp < end])


async def _expand(pattern, limit):
    return (["X:INTLK:CHL01"] if pattern == "X:INTLK:*" else []), False


async def test_facts_and_report():
    resolution = resolve(CONFIG, inventory([VALUES]))
    facts = await DailyReport(_Archiver(), _expand).facts(CONFIG, resolution, START, END)
    mag = facts["groups"]["magnets"]
    sol = mag["rows"][0]
    assert sol["setpoint"]["first"] == 100.0 and sol["setpoint"]["last"] == 110.3 and len(sol["setpoint"]["changes"]) == 2
    kinds = {(f["device"], f["kind"]) for g in facts["groups"].values() for f in g.get("flags", [])}
    assert ("GUNSOL01", "state changes") in kinds and ("GUNSOL01", "readback off setpoint") in kinds
    assert ("VGA01", "pressure spike") in kinds
    assert not any(k[1].startswith("high limit") for k in kinds)
    assert [e["time"] for e in facts["trips"]["interlock"]] == ["2026-11-27T14:00:00Z"]
    assert facts["groups"]["pointing"]["cameras"][0]["camera"] == "CAM01"
    assert facts["pvs_without_history"] == 0

    title, body = render(facts)
    assert title == "X daily report 2026-11-27"
    assert "| GUNSOL01 | solenoid | 2 | 100 | 110 |" in body
    assert "| FI3-HMN-01 |  | 2 | 11 | 12.5 |" in body          # two moves
    assert "| CAM01 | 6 | 3 |" in body                            # acquired 6 h at 3 Hz
    assert "| CAM01 | 31 | - |" in body                           # centroid X range 299..330
    assert "| interlock | 1 |" in body


async def test_missing_history_is_a_gap_not_an_error():
    resolution = resolve(CONFIG, inventory([VALUES]))

    class Empty:
        async def get_data(self, *a, **k):
            raise RuntimeError("not archived")

    facts = await DailyReport(Empty(), _expand).facts(CONFIG, resolution, START, END)
    assert facts["pvs_without_history"] == facts["pvs_read"] > 0
    assert "Data gaps" in render(facts)[1]


def test_day_window():
    assert day_window("2026-11-27") == (START, END)


async def test_an_unreadable_logbook_is_said_in_the_report():
    resolution = resolve(CONFIG, inventory([VALUES]))

    class Refusing:
        async def search_entries(self, **kw):
            raise RuntimeError("Olog refused to search the logbook (HTTP 403)")

    facts = await DailyReport(_Archiver(), _expand, logbook=Refusing()).facts(CONFIG, resolution, START, END)
    assert "could not be read: Olog refused" in render(facts)[1]


async def test_an_off_supply_is_not_flagged_for_its_readback():
    history = dict(HISTORY)
    history["X:MAG:GUNSOL01:STATE_RB"] = [_s(-48, 0)]                         # OFF all day

    class Archiver(_Archiver):
        async def get_data(self, pv, start, end, *, max_points=None, timeout=8.0):
            return ArchiverSeries(pv_name=pv, samples=[s for s in history.get(pv, []) if s.timestamp < end])

    facts = await DailyReport(Archiver(), _expand).facts(CONFIG, resolve(CONFIG, inventory([VALUES])), START, END)
    kinds = {f["kind"] for f in facts["groups"]["magnets"]["flags"]}
    assert "readback off setpoint" not in kinds


def test_no_beam_is_said_plainly():
    facts = {"beamline": "X", "start": "2026-11-27T00:00:00Z", "end": "2026-11-28T00:00:00Z", "pvs_read": 1,
             "pvs_without_history": 0, "gaps": [], "groups": {}, "trips": {}, "logbook": [],
             "beam": {"pv": "X:CHARGE", "above": 10, "mean": 0.1, "max": 0.2, "bins_with_data": 288}}
    assert "No beam: `X:CHARGE` never reached 10" in render(facts)[1]
