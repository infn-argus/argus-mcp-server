from __future__ import annotations

from dataclasses import dataclass

import pytest

from argus.services.report_service import ReportConfigError, ReportService, inventory, required_pvs, resolve

VALUES = """
iocDefaults:
  haz: {devgroup: mag, devtype: haz-ser}
epicsConfiguration:
  iocs:
    haz-ch1:
      template: haz
      iocprefix: SPARC:MAG:HZ
      zones: LINAC
      devices: [{name: GUNSOL01}, {name: SBNQUA04, zones: [SABINA]}]
    vac-ptl:
      devgroup: vac
      devtype: ipcmini
      iocprefix: SPARC:VAC
      iocroot: PTLVPC
      devices: [{name: RFDSIP01}]
    old-cam:
      devgroup: cam
      iocprefix: SPARC:CAM
      disable: true
      devices: [{name: OLD01}]
    undulator1:
      devgroup: mag
      devtype: kima
      iocprefix: SPARC:SBN
      iocroot: UND01
"""
SOFT = """
softiocDefaults: {devgroup: global}
softiocs:
  - {name: softinterlock, iocprefix: SPARC:SOFTINTLK}
"""

CONFIG = {
    "beamline": "SPARC",
    "interfaces": {
        "magnet": {"readback": "{pv}:CURRENT_RB", "state": "{pv}:STATE_RB"},
        "undulator": {"gap": "{pv}:GAP_RB", "alarms": "{pv}:ALARM_*"},
        "ion_pump": {"pressure": "{pv}:PRES_RB"},
    },
    "naming": {"pattern": r"^(?P<section>[A-Z0-9]{3})(?P<type>[A-Z]{3})"},
    "groups": {
        "magnets": {"select": {"devgroup": "mag", "exclude_devtype": ["kima"]}, "interface": "magnet",
                    "function_from": "type", "functions": {"SOL": "solenoid"}},
        "undulators": {"select": {"devtype": "kima"}, "interface": "undulator"},
        "vacuum": {"select": {"devgroup": "vac"}, "interface_by_devtype": {"ipcmini": "ion_pump"}},
        "pointing": {"group_by": "zone"},
    },
    "key_pvs": {"charge": "SPARC:DIAG:TURBOBCM:CHARGE_RB", "interlocks": "SPARC:CHL:*:ALARM:ILK_*"},
    "trips": [{"name": "fault", "watch": ["{magnets}:STATE_RB"]}],
    "paths": {"shared": {"sections": ["GUN"]}, "sabina": {"zones": ["SABINA"], "devices": ["undulator1"]}},
}


def test_inventory_merges_defaults_softiocs_and_iocroot_and_skips_disabled():
    devices = {d.name: d for d in inventory([VALUES, SOFT])}
    assert devices["GUNSOL01"].pv == "SPARC:MAG:HZ:GUNSOL01" and devices["GUNSOL01"].devgroup == "mag"
    assert devices["GUNSOL01"].zones == ["LINAC"] and devices["SBNQUA04"].zones == ["SABINA"]
    assert devices["RFDSIP01"].pv == "SPARC:VAC:PTLVPC:RFDSIP01"
    assert devices["undulator1"].pv == "SPARC:SBN:UND01"          # an IOC with no devices is one device
    assert devices["softinterlock"].devgroup == "global"          # softiocs are configured IOCs too
    assert "OLD01" not in devices


def test_resolve_builds_fields_functions_and_paths():
    res = resolve(CONFIG, inventory([VALUES, SOFT]))
    by = {d.device.name: d for d in res.devices}
    assert res.counts() == {"magnets": 2, "undulators": 1, "vacuum": 1}
    assert by["GUNSOL01"].fields == {"readback": "SPARC:MAG:HZ:GUNSOL01:CURRENT_RB",
                                     "state": "SPARC:MAG:HZ:GUNSOL01:STATE_RB"}
    assert by["GUNSOL01"].function == "solenoid" and by["GUNSOL01"].paths == ["shared"]
    assert res.no_function == ["SBNQUA04"]                        # QUA is not in this config's functions
    assert by["SBNQUA04"].paths == ["sabina"] and by["undulator1"].paths == ["sabina"]
    assert res.no_path == ["RFDSIP01"]
    assert res.key_pvs == ["SPARC:DIAG:TURBOBCM:CHARGE_RB"]
    assert res.key_patterns == ["SPARC:CHL:*:ALARM:ILK_*"]       # {magnets} placeholders are not PVs


async def test_required_pvs_expands_patterns_and_reports_empty_ones():
    res = resolve(CONFIG, inventory([VALUES, SOFT]))
    known = {"SPARC:CHL:*:ALARM:ILK_*": ["SPARC:CHL:CHLAC101:ALARM:ILK_FLOW_LLIM"]}

    async def expand(pattern, limit):
        return known.get(pattern, []), False

    pvs, empty = await required_pvs(res, expand)
    assert "SPARC:SBN:UND01:GAP_RB" in pvs and "SPARC:CHL:CHLAC101:ALARM:ILK_FLOW_LLIM" in pvs
    assert "SPARC:DIAG:TURBOBCM:CHARGE_RB" in pvs and len(pvs) == len(set(pvs))
    assert empty == ["SPARC:SBN:UND01:ALARM_*"]


async def test_required_pvs_refuses_a_pattern_that_matches_too_much():
    res = resolve(CONFIG, inventory([VALUES]))

    async def expand(pattern, limit):
        return ["X"], True

    with pytest.raises(ReportConfigError, match="matches more than"):
        await required_pvs(res, expand)


@dataclass
class _File:
    content: str


@dataclass
class _Value:
    name: str
    status: str


class _Knowledge:
    def __init__(self, files):
        self.files = files

    async def get_file(self, repo, path, ref):
        from argus.core.errors import ArgusError
        if path not in self.files:
            raise ArgusError("404")
        return _File(self.files[path])


class _Ops:
    def __init__(self, silent=()):
        self.silent = set(silent)

    async def pv_names_matching(self, pattern, limit):
        return ([], False)

    async def get_pvs(self, names, timeout=5.0):
        return [_Value(n, "disconnected" if n in self.silent else "connected") for n in names]


class _Archiver:
    def __init__(self, archived=()):
        self.archived = set(archived)
        self.requested: list[str] = []

    async def pv_status(self, names):
        return {n: ("Being archived" if n in self.archived else "Not being archived") for n in names}

    async def archive_pvs(self, names):
        self.requested += names
        return {n: "Archive request submitted" for n in names}


def _service(archiver, silent=()):
    import yaml
    config = {**CONFIG, "values_files": ["deploy/values.yaml", "deploy/values-softiocs.yaml"]}
    files = {"deploy/argus-report.yaml": yaml.safe_dump(config), "deploy/values.yaml": VALUES,
             "deploy/values-softiocs.yaml": SOFT}
    return ReportService(knowledge=_Knowledge(files), operations=_Ops(silent), archiver=archiver)


async def test_coverage_splits_archived_missing_and_silent():
    archiver = _Archiver(archived={"SPARC:MAG:HZ:GUNSOL01:CURRENT_RB"})
    cov = await _service(archiver, silent={"SPARC:SBN:UND01:GAP_RB"}).coverage()
    assert cov.archived == ["SPARC:MAG:HZ:GUNSOL01:CURRENT_RB"]
    assert "SPARC:SBN:UND01:GAP_RB" in cov.not_answering and "SPARC:SBN:UND01:GAP_RB" not in cov.not_archived
    assert "SPARC:VAC:PTLVPC:RFDSIP01:PRES_RB" in cov.not_archived


async def test_archive_missing_requests_only_answering_unarchived_pvs():
    archiver = _Archiver(archived={"SPARC:MAG:HZ:GUNSOL01:CURRENT_RB"})
    result = await _service(archiver, silent={"SPARC:SBN:UND01:GAP_RB"}).archive_missing()
    assert "SPARC:MAG:HZ:GUNSOL01:CURRENT_RB" not in archiver.requested
    assert "SPARC:SBN:UND01:GAP_RB" not in archiver.requested
    assert "SPARC:VAC:PTLVPC:RFDSIP01:PRES_RB" in archiver.requested
    assert result["requested"] == len(archiver.requested) and "not_accepted" not in result


async def test_missing_report_config_is_a_clear_error():
    service = ReportService(knowledge=_Knowledge({}), operations=_Ops(), archiver=_Archiver())
    with pytest.raises(ReportConfigError, match="argus-report.yaml"):
        await service.coverage()
