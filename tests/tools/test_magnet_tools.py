"""Magnet sets end to end through the tools: a file into save-and-restore, onto the machine, into the archiver."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from argus.config.settings import ArchiverSettings, SaveRestoreSettings
from argus.core.context import AppContext
from argus.core.request_context import confirm_writes_scope
from argus.mcp_server.server import build_registry
from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.saverestore.saverestore import ROOT_FOLDER_ID, SaveRestoreProvider
from argus.services.magnet_service import MagnetService
from argus.services.saverestore_service import SaveRestoreService
from tests.services.test_magnet_sets import Supplies

SAR = "http://sar.test/save-restore"
ARCH = "http://arch.test"
FOLDER = "11111111-1111-1111-1111-111111111111"
CONFIG = "22222222-2222-2222-2222-222222222222"
SNAP = "33333333-3333-3333-3333-333333333333"

CSV = "Name,Prefix,Current,State\nQUATM001,BTF:MAG:OCEME642,0.331522,ON\nCHHTM001,BTF:MAG:EASY,0.0,STANDBY\n"
LABELS = ["OFF", "STANDBY", "ON"]


def _node(uid: str, name: str, node_type: str) -> dict:
    return {"uniqueId": uid, "name": name, "nodeType": node_type, "userName": "Jane Operator"}


def _supplies() -> Supplies:
    s = Supplies()
    s.add("BTF:MAG:OCEME642:QUATM001", "STANDBY", 0.0)
    s.add("BTF:MAG:EASY:CHHTM001", "ON", 2.0)
    return s


def _ctx(app_context: AppContext, supplies: Supplies) -> AppContext:
    snr = SaveRestoreProvider(SaveRestoreSettings(_env_file=None, SAVERESTORE_BASE_URL=SAR))
    archiver = ArchiverApplianceProvider(ArchiverSettings(_env_file=None, ARCHIVER_BASE_URL=ARCH))
    snr_service = SaveRestoreService(snr)
    magnets = MagnetService(snr_service, snr, supplies, archiver)
    return AppContext(**{**app_context.__dict__, "saverestore_service": snr_service, "magnet_service": magnets})


def _payload(response) -> dict:
    return json.loads(response[0].text)


def _tree(configs: list[dict] | None = None, snapshots: list[dict] | None = None) -> None:
    respx.get(f"{SAR}/node/{ROOT_FOLDER_ID}/children").mock(return_value=httpx.Response(200, json=[_node(FOLDER, "BTF_CONF", "FOLDER")]))
    respx.get(f"{SAR}/node/{FOLDER}/children").mock(return_value=httpx.Response(200, json=configs or []))
    respx.get(f"{SAR}/node/{CONFIG}/children").mock(return_value=httpx.Response(200, json=snapshots or []))


def _live_items() -> list[dict]:
    """What the service reads now: one supply connected (with its display and states), one not."""
    live_double = {"type": {"name": "VDouble", "version": 1}, "value": 0.0, "alarm": {"severity": "NONE"},
                   "time": {"unixSec": 1}, "display": {"units": "A", "lowDisplay": -10, "highDisplay": 10}}
    live_enum = {"type": {"name": "VEnum", "version": 1}, "value": 1, "alarm": {"severity": "NONE"},
                 "time": {"unixSec": 1}, "enum": {"labels": LABELS}}
    pv = lambda base, f, rb: {"pvName": f"{base}:{f}", "readbackPvName": f"{base}:{rb}", "readOnly": False}  # noqa: E731
    q, c = "BTF:MAG:OCEME642:QUATM001", "BTF:MAG:EASY:CHHTM001"
    return [
        {"configPv": pv(q, "CURRENT_SP", "CURRENT_RB"), "value": live_double, "readbackValue": live_double},
        {"configPv": pv(q, "STATE_SP", "STATE_RB"), "value": live_enum, "readbackValue": live_enum},
        {"configPv": pv(c, "CURRENT_SP", "CURRENT_RB"), "value": None},
        {"configPv": pv(c, "STATE_SP", "STATE_RB"), "value": None},
    ]


@respx.mock
async def test_a_file_becomes_a_configuration_and_a_snapshot_of_the_files_values(app_context: AppContext):
    supplies = _supplies()
    ctx = _ctx(app_context, supplies)
    _tree()
    created = respx.put(f"{SAR}/config").mock(return_value=httpx.Response(
        200, json={"configurationNode": _node(CONFIG, "pTLp", "CONFIGURATION")}))
    respx.get(f"{SAR}/take-snapshot/{CONFIG}").mock(return_value=httpx.Response(200, json=_live_items()))
    saved = respx.put(f"{SAR}/snapshot").mock(return_value=httpx.Response(200, json={"snapshotNode": _node(SNAP, "pTLp", "SNAPSHOT")}))

    registry = build_registry()
    args = {"file_text": CSV, "name": "pTLp", "folder": "BTF_CONF"}
    with confirm_writes_scope(True):
        first = _payload(await registry.dispatch("create_magnet_set", dict(args), ctx))
        assert first["status"] == "confirmation_required"
        assert "create the configuration /BTF_CONF/pTLp (4 PVs" in first["action"]
        assert not created.called
        done = _payload(await registry.dispatch("create_magnet_set", {**args, "confirmation_id": first["confirmation_id"]}, ctx))

    assert done["status"] == "success", done
    assert done["configuration"]["created"] and done["snapshot"]["name"] == "pTLp"
    pvs = json.loads(created.calls[0].request.content)["configurationData"]["pvList"]
    assert {"pvName": "BTF:MAG:EASY:CHHTM001:STATE_SP", "readbackPvName": "BTF:MAG:EASY:CHHTM001:STATE_RB", "readOnly": False} in pvs
    items = {i["configPv"]["pvName"]: i for i in json.loads(saved.calls[0].request.content)["snapshotData"]["snapshotItems"]}
    q = items["BTF:MAG:OCEME642:QUATM001:CURRENT_SP"]["value"]
    assert q["value"] == 0.331522 and q["display"]["units"] == "A"  # the file's value, in the live form
    assert items["BTF:MAG:OCEME642:QUATM001:STATE_SP"]["value"]["value"] == LABELS.index("ON")
    # Not connected: built from scratch, the states read from the PV.
    built = items["BTF:MAG:EASY:CHHTM001:CURRENT_SP"]["value"]
    assert built["type"]["name"] == "VDouble" and built["value"] == 0.0
    assert items["BTF:MAG:EASY:CHHTM001:STATE_SP"]["value"]["value"] == ["OFF", "STANDBY", "ON"].index("STANDBY")
    assert all(i["readbackValue"] is None for i in items.values())
    assert supplies.writes == []  # the machine is not touched
    assert "logbook_suggestion" in done


@respx.mock
async def test_an_existing_configuration_with_other_pvs_or_a_taken_snapshot_name_is_refused(app_context: AppContext):
    ctx = _ctx(app_context, _supplies())
    _tree(configs=[_node(CONFIG, "pTLp", "CONFIGURATION")], snapshots=[_node(SNAP, "pTLp", "SNAPSHOT")])
    respx.get(f"{SAR}/config/{CONFIG}").mock(return_value=httpx.Response(200, json={
        "uniqueId": CONFIG, "pvList": [{"pvName": "OTHER:PV"}]}))
    registry = build_registry()
    refused = _payload(await registry.dispatch("create_magnet_set", {"file_text": CSV, "name": "pTLp", "folder": "BTF_CONF"}, ctx))
    assert refused["status"] == "error" and "already exists with other PVs" in refused["message"]
    assert "another name" in refused["message"]


def _snapshot_with(values: dict[str, object]) -> list[dict]:
    items = []
    for pv, value in values.items():
        vtype = ({"type": {"name": "VEnum"}, "value": LABELS.index(value), "enum": {"labels": LABELS}}
                 if isinstance(value, str) else {"type": {"name": "VDouble"}, "value": value})
        items.append({"configPv": {"pvName": pv}, "value": vtype})
    return items


def _mock_snapshot(values: dict[str, object]) -> None:
    respx.get(f"{SAR}/node/{SNAP}").mock(return_value=httpx.Response(200, json=_node(SNAP, "pTLp", "SNAPSHOT")))
    respx.get(f"{SAR}/snapshot/{SNAP}").mock(return_value=httpx.Response(200, json={"uniqueId": SNAP, "snapshotItems": _snapshot_with(values)}))


SET = {
    "BTF:MAG:OCEME642:QUATM001:CURRENT_SP": 4.0, "BTF:MAG:OCEME642:QUATM001:STATE_SP": "ON",
    "BTF:MAG:EASY:CHHTM001:CURRENT_SP": 0.0, "BTF:MAG:EASY:CHHTM001:STATE_SP": "STANDBY",
}


@respx.mock
async def test_putting_a_set_on_the_machine_is_confirmed_for_every_client_and_shows_each_move(app_context: AppContext):
    supplies = _supplies()
    ctx = _ctx(app_context, supplies)
    _mock_snapshot(SET)
    registry = build_registry()
    args = {"action": "set", "snapshot": SNAP, "timeout_s": 2}
    first = _payload(await registry.dispatch("apply_magnet_set", dict(args), ctx))  # no confirming header
    assert first["status"] == "confirmation_required"
    assert "QUATM001: STANDBY 0 A -> ON 4 A" in first["action"]
    assert "CHHTM001: ON 2 A -> STANDBY 0 A" in first["action"]
    assert supplies.writes == []
    done = _payload(await registry.dispatch("apply_magnet_set", {**args, "confirmation_id": first["confirmation_id"]}, ctx))
    assert done["status"] == "success" and done["complete"], done
    assert supplies.state == {"BTF:MAG:OCEME642:QUATM001": "ON", "BTF:MAG:EASY:CHHTM001": "STANDBY"}
    assert supplies.rb["BTF:MAG:OCEME642:QUATM001"] == 4.0


@respx.mock
async def test_a_target_outside_the_supplys_limits_is_refused_before_anything_is_confirmed(app_context: AppContext):
    supplies = _supplies()
    supplies.limits["BTF:MAG:OCEME642:QUATM001"] = (-3.0, 3.0)
    ctx = _ctx(app_context, supplies)
    _mock_snapshot(SET)
    answer = _payload(await build_registry().dispatch("apply_magnet_set", {"action": "set", "snapshot": SNAP}, ctx))
    assert answer["status"] == "error" and "outside its limits [-3.0, 3.0]" in answer["message"]


@respx.mock
async def test_pretune_moves_a_fraction_each_step_and_switches_off_only_on_the_last(app_context: AppContext):
    supplies = _supplies()
    ctx = _ctx(app_context, supplies)
    _mock_snapshot(SET)
    registry = build_registry()

    async def step(left: int) -> dict:
        args = {"action": "pretune", "snapshot": SNAP, "steps_left": left, "timeout_s": 2}
        first = _payload(await registry.dispatch("apply_magnet_set", dict(args), ctx))
        assert first["status"] == "confirmation_required", first
        assert ("the LAST step" if left == 1 else f"1/{left} of the way") in first["action"]
        return _payload(await registry.dispatch("apply_magnet_set", {**args, "confirmation_id": first["confirmation_id"]}, ctx))

    one = await step(2)
    assert one["complete"] and one["steps_left_after"] == 1
    assert supplies.sp["BTF:MAG:OCEME642:QUATM001"] == 2.0  # halfway, and switched ON on the first step
    assert supplies.state["BTF:MAG:EASY:CHHTM001"] == "ON" and supplies.sp["BTF:MAG:EASY:CHHTM001"] == 1.0
    last = await step(1)
    assert last["complete"] and "logbook_suggestion" in last
    assert supplies.sp["BTF:MAG:OCEME642:QUATM001"] == 4.0
    assert supplies.state["BTF:MAG:EASY:CHHTM001"] == "STANDBY"


@respx.mock
async def test_all_standby_and_all_on_act_on_every_magnet_of_a_configuration(app_context: AppContext):
    supplies = _supplies()
    ctx = _ctx(app_context, supplies)
    _tree(configs=[_node(CONFIG, "pTLp", "CONFIGURATION")])
    respx.get(f"{SAR}/config/{CONFIG}").mock(return_value=httpx.Response(200, json={
        "uniqueId": CONFIG, "pvList": [{"pvName": p} for p in SET]}))
    registry = build_registry()
    for action, expected in (("standby", {"STANDBY"}), ("on", {"ON"})):
        args = {"action": action, "configuration": "BTF_CONF/pTLp", "timeout_s": 2}
        first = _payload(await registry.dispatch("apply_magnet_set", dict(args), ctx))
        done = _payload(await registry.dispatch("apply_magnet_set", {**args, "confirmation_id": first["confirmation_id"]}, ctx))
        assert done["complete"], done
        assert set(supplies.state.values()) == expected
    assert supplies.sp["BTF:MAG:EASY:CHHTM001"] == 0.0  # zeroed before standby, left at 0 by 'on'


@respx.mock
async def test_archiving_asks_only_for_the_pvs_not_archived_yet(app_context: AppContext):
    ctx = _ctx(app_context, _supplies())
    pvs = [f"BTF:MAG:OCEME642:QUATM001:{f}" for f in ("CURRENT_SP", "CURRENT_RB", "STATE_SP", "STATE_RB")]
    pvs += [f"BTF:MAG:EASY:CHHTM001:{f}" for f in ("CURRENT_SP", "CURRENT_RB", "STATE_SP", "STATE_RB")]
    respx.get(f"{ARCH}/mgmt/bpl/getPVStatus").mock(return_value=httpx.Response(200, json=[
        {"pvName": p, "status": "Being archived" if p.endswith("CURRENT_RB") else "Not being archived"} for p in pvs]))
    archive = respx.post(f"{ARCH}/mgmt/bpl/archivePV").mock(side_effect=lambda req: httpx.Response(
        200, json=[{"pvName": r["pv"], "status": "Archive request submitted"} for r in json.loads(req.content)]))
    registry = build_registry()
    with confirm_writes_scope(True):
        first = _payload(await registry.dispatch("archive_magnet_pvs", {"file_text": CSV}, ctx))
        assert "start archiving 6 PV(s)" in first["action"] and "2 are already archived" in first["action"]
        done = _payload(await registry.dispatch("archive_magnet_pvs", {"file_text": CSV, "confirmation_id": first["confirmation_id"]}, ctx))
    asked = [r["pv"] for r in json.loads(archive.calls[0].request.content)]
    assert len(asked) == 6 and not any(p.endswith("CURRENT_RB") for p in asked)
    assert len(done["already_archived"]) == 2


@pytest.mark.parametrize("left", [0, -1])
@respx.mock
async def test_a_pretune_needs_a_step_count_of_at_least_one(app_context: AppContext, left: int):
    ctx = _ctx(app_context, _supplies())
    _mock_snapshot(SET)
    answer = _payload(await build_registry().dispatch("apply_magnet_set", {"action": "pretune", "snapshot": SNAP, "steps_left": left}, ctx))
    assert answer["status"] == "error" and "at least 1" in answer["message"]
