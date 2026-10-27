"""Creating save-and-restore configurations and snapshots, as the signed-in user, with confirmation."""

import json

import httpx
import pytest
import respx

from argus.config.settings import SaveRestoreSettings
from argus.core.context import AppContext
from argus.core.request_context import caller_token_scope, confirm_writes_scope
from argus.mcp_server.server import build_registry
from argus.providers.saverestore.exceptions import SaveRestoreQueryError, SaveRestoreRefusedError
from argus.providers.saverestore.saverestore import ROOT_FOLDER_ID, SaveRestoreProvider
from argus.services.beamline_inventory_service import BeamlineDevice, InventoryListing
from argus.services.device_types import get_profile
from argus.services.saverestore_service import SaveRestoreService

SAR = "http://sar.test/save-restore"
FOLDER = "11111111-1111-1111-1111-111111111111"
CONFIG = "22222222-2222-2222-2222-222222222222"


def _device(name: str, devgroup: str, prefix: str) -> BeamlineDevice:
    profile = get_profile(devgroup)
    return BeamlineDevice(
        name=name, ioc_name="ioc", iocprefix=prefix, devgroup=devgroup, devtype=None, devfunc=None, zones=["ALL"],
        key_pvs=profile.key_pvs(prefix, name) if profile else {},
    )


class _Inventory:
    async def list_inventory(self, **_: object) -> InventoryListing:
        return InventoryListing(
            source="yaml",
            devices=[_device("QUATB001", "mag", "BTF:MAG:DANFYSIK"), _device("VGTB01", "vac", "BTF:VAC")],
        )


def _service() -> SaveRestoreService:
    return SaveRestoreService(SaveRestoreProvider(SaveRestoreSettings(_env_file=None, SAVERESTORE_BASE_URL=SAR)), _Inventory())


def _ctx(app_context: AppContext) -> AppContext:
    return AppContext(**{**app_context.__dict__, "saverestore_service": _service()})


def _node(uid: str, name: str, node_type: str, user: str | None = None) -> dict:
    return {"uniqueId": uid, "name": name, "nodeType": node_type, "userName": user}


def _mock_tree() -> None:
    respx.get(f"{SAR}/node/{ROOT_FOLDER_ID}/children").mock(
        return_value=httpx.Response(200, json=[_node(FOLDER, "BTF_CONF", "FOLDER")])
    )
    respx.get(f"{SAR}/node/{FOLDER}/children").mock(
        return_value=httpx.Response(200, json=[_node(CONFIG, "Quadrupoles", "CONFIGURATION")])
    )


# --- planning ------------------------------------------------------------

async def test_a_magnet_gives_its_setpoints_with_their_readbacks_and_a_gauge_is_skipped_with_a_reason():
    plan = await _service().plan_configuration(["quatb001", "VGTB01", "NOPE"], pvs=["BTF:MOT:X:VAL", {"setpoint": "A:SP", "readback": "A:RB"}])
    pairs = {(pv.pv_name, pv.readback_pv_name) for pv in plan.pv_list}
    assert ("BTF:MAG:DANFYSIK:QUATB001:CURRENT_SP", "BTF:MAG:DANFYSIK:QUATB001:CURRENT_RB") in pairs
    assert ("BTF:MAG:DANFYSIK:QUATB001:STATE_SP", "BTF:MAG:DANFYSIK:QUATB001:STATE_RB") in pairs
    assert ("BTF:MOT:X:VAL", None) in pairs and ("A:SP", "A:RB") in pairs
    assert "no setpoint convention" in plan.skipped["VGTB01"]
    assert "not in this beamline's device inventory" in plan.skipped["NOPE"]


async def test_without_readbacks_only_setpoints_and_no_duplicates():
    plan = await _service().plan_configuration(
        ["QUATB001"], include_readbacks=False, pvs=["BTF:MAG:DANFYSIK:QUATB001:CURRENT_SP"]
    )
    assert [pv.readback_pv_name for pv in plan.pv_list] == [None, None]
    assert len(plan.pv_list) == 2


# --- provider ------------------------------------------------------------

@respx.mock
async def test_folders_resolve_by_path_in_any_case_and_a_wrong_one_lists_what_exists():
    _mock_tree()
    provider = SaveRestoreProvider(SaveRestoreSettings(_env_file=None, SAVERESTORE_BASE_URL=SAR))
    assert await provider.resolve_folder("/btf_conf") == (FOLDER, "/BTF_CONF")
    with pytest.raises(SaveRestoreQueryError, match="No folder 'SPARC' in the root. Folders there: BTF_CONF."):
        await provider.resolve_folder("SPARC/Magnets")


@respx.mock
async def test_a_refusal_says_why_and_what_signing_in_needs():
    respx.put(f"{SAR}/config").mock(return_value=httpx.Response(401, json={"error": "Unauthorized"}))
    provider = SaveRestoreProvider(SaveRestoreSettings(_env_file=None, SAVERESTORE_BASE_URL=SAR))
    with pytest.raises(SaveRestoreRefusedError, match=r"HTTP 401\): Unauthorized - creating needs the signed-in user's token"):
        await provider.create_configuration(FOLDER, "Q", [])


# --- end to end, confirmed -------------------------------------------------

@respx.mock
async def test_create_configuration_previews_the_real_pvs_then_creates_it_as_the_user(app_context: AppContext):
    _mock_tree()
    created = respx.put(f"{SAR}/config").mock(
        return_value=httpx.Response(
            200, json={"configurationNode": _node("33333333-3333-3333-3333-333333333333", "Quads", "CONFIGURATION", "jdoe")}
        )
    )
    ctx, registry = _ctx(app_context), build_registry()
    args = {"name": "Quads", "folder": "BTF_CONF", "devices": ["QUATB001", "VGTB01"]}
    with confirm_writes_scope(True), caller_token_scope("eyJuser"):
        first = json.loads((await registry.dispatch("create_saverestore_configuration", dict(args), ctx))[0].text)
        assert first["status"] == "confirmation_required" and not created.called
        assert "configuration /BTF_CONF/Quads under your name, with 2 PV(s)" in first["action"]
        assert "BTF:MAG:DANFYSIK:QUATB001:CURRENT_SP (readback BTF:MAG:DANFYSIK:QUATB001:CURRENT_RB)" in first["action"]
        assert "Not included: VGTB01" in first["action"]
        done = json.loads((await registry.dispatch(
            "create_saverestore_configuration", {**args, "confirmation_id": first["confirmation_id"]}, ctx
        ))[0].text)

    assert done["status"] == "success" and done["configuration"]["path"] == "/BTF_CONF/Quads"
    assert done["configuration"]["owner"] == "jdoe" and done["pv_count"] == 2
    assert "ASK whether to record it in the logbook" in done["next_step"]
    assert "/BTF_CONF/Quads (id 33333333" in done["logbook_suggestion"]["text"]
    request = created.calls.last.request
    assert request.url.params["parentNodeId"] == FOLDER
    assert request.headers["authorization"] == "Bearer eyJuser"
    body = json.loads(request.content)
    assert body["configurationNode"]["nodeType"] == "CONFIGURATION"
    assert {"pvName": "BTF:MAG:DANFYSIK:QUATB001:STATE_SP", "readbackPvName": "BTF:MAG:DANFYSIK:QUATB001:STATE_RB",
            "readOnly": False} in body["configurationData"]["pvList"]


@respx.mock
async def test_a_wrong_folder_fails_at_the_preview_before_any_confirmation(app_context: AppContext):
    _mock_tree()
    with confirm_writes_scope(True):
        payload = json.loads((await build_registry().dispatch(
            "create_saverestore_configuration", {"name": "Q", "folder": "NOPE", "devices": ["QUATB001"]}, _ctx(app_context)
        ))[0].text)
    assert payload["status"] == "error" and "Folders there: BTF_CONF" in payload["message"]
    assert "confirmation_id" not in payload


@respx.mock
async def test_a_snapshot_of_a_configuration_by_path_is_read_by_the_service_then_saved_as_the_user(app_context: AppContext):
    _mock_tree()
    items = [
        {"configPv": {"pvName": "A:SP"}, "value": {"value": 4.2}, "readbackValue": {"value": 4.19}},
        {"configPv": {"pvName": "B:SP"}, "value": None},
    ]
    taken = respx.get(f"{SAR}/take-snapshot/{CONFIG}").mock(return_value=httpx.Response(200, json=items))
    saved = respx.put(f"{SAR}/snapshot").mock(
        return_value=httpx.Response(
            200, json={"snapshotNode": _node("44444444-4444-4444-4444-444444444444", "before energy change", "SNAPSHOT", "jdoe")}
        )
    )
    ctx, registry = _ctx(app_context), build_registry()
    args = {"configuration": "BTF_CONF/quadrupoles", "name": "before energy change", "comment": "ramp to 500 MeV"}
    with confirm_writes_scope(True), caller_token_scope("eyJuser"):
        first = json.loads((await registry.dispatch("take_saverestore_snapshot", dict(args), ctx))[0].text)
        assert first["status"] == "confirmation_required" and not taken.called and not saved.called
        assert "of the configuration /BTF_CONF/Quadrupoles under your name" in first["action"]
        done = json.loads((await registry.dispatch(
            "take_saverestore_snapshot", {**args, "confirmation_id": first["confirmation_id"]}, ctx
        ))[0].text)

    assert done["status"] == "success" and done["pv_count"] == 2 and done["not_connected"] == ["B:SP"]
    assert done["snapshot"]["owner"] == "jdoe"
    text = done["logbook_suggestion"]["text"]
    assert "snapshot 'before energy change' (id 44444444" in text and "configuration /BTF_CONF/Quadrupoles" in text
    assert "1 not connected (B:SP)" in text and "Comment: ramp to 500 MeV" in text
    body = json.loads(saved.calls.last.request.content)
    assert saved.calls.last.request.url.params["parentNodeId"] == CONFIG
    assert body["snapshotNode"]["nodeType"] == "SNAPSHOT" and body["snapshotData"]["snapshotItems"] == items
    assert saved.calls.last.request.headers["authorization"] == "Bearer eyJuser"


# --- listing snapshots and restoring -----------------------------------------

SNAP_OLD = "55555555-5555-5555-5555-555555555555"
SNAP_NEW = "66666666-6666-6666-6666-666666666666"


def _mock_snapshots() -> None:
    _mock_tree()
    respx.get(f"{SAR}/node/{CONFIG}/children").mock(
        return_value=httpx.Response(
            200,
            json=[
                {**_node(SNAP_OLD, "monday", "SNAPSHOT", "asmith"), "created": 1792800000000, "description": "before"},
                {**_node(SNAP_NEW, "tuesday", "SNAPSHOT", "jdoe"), "created": 1792900000000, "description": "after ramp"},
            ],
        )
    )


@respx.mock
async def test_a_configurations_snapshots_are_listed_newest_first(app_context: AppContext):
    _mock_snapshots()
    payload = json.loads((await build_registry().dispatch(
        "search_snapshots", {"configuration": "BTF_CONF/Quadrupoles"}, _ctx(app_context)
    ))[0].text)
    assert payload["configuration"]["path"] == "/BTF_CONF/Quadrupoles"
    assert [s["name"] for s in payload["snapshots"]] == ["tuesday", "monday"]
    assert payload["snapshots"][0]["owner"] == "jdoe" and payload["snapshots"][0]["comment"] == "after ramp"


@respx.mock
async def test_snapshots_resolve_by_name_or_latest_and_a_wrong_name_lists_them():
    _mock_snapshots()
    service = _service()
    assert (await service.resolve_snapshot("latest", "BTF_CONF/Quadrupoles"))[0].unique_id == SNAP_NEW
    assert (await service.resolve_snapshot("MONDAY", "BTF_CONF/Quadrupoles"))[0].unique_id == SNAP_OLD
    with pytest.raises(SaveRestoreQueryError, match="newest first: tuesday, monday"):
        await service.resolve_snapshot("friday", "BTF_CONF/Quadrupoles")


@respx.mock
async def test_restore_is_confirmed_even_without_the_header_and_previews_what_would_change(app_context: AppContext):
    _mock_snapshots()
    respx.get(f"{SAR}/compare/{SNAP_NEW}").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"pvName": "A:SP", "equal": False, "storedValue": {"value": 4.2}, "liveValue": {"value": 5.0}, "delta": "-0.8"},
                {"pvName": "B:SP", "equal": True, "storedValue": {"value": 1}, "liveValue": {"value": 1}},
            ],
        )
    )
    restored = respx.post(f"{SAR}/restore/node").mock(
        return_value=httpx.Response(
            200, json=[{"snapshotItem": {"configPv": {"pvName": "A:SP"}}, "errorMsg": "Failed to write to PV 'A:SP'"}]
        )
    )
    ctx, registry = _ctx(app_context), build_registry()
    args = {"snapshot": "latest", "configuration": "BTF_CONF/Quadrupoles"}
    with caller_token_scope("eyJuser"):  # no confirm_writes_scope: restore asks anyway
        first = json.loads((await registry.dispatch("restore_saverestore_snapshot", dict(args), ctx))[0].text)
        assert first["status"] == "confirmation_required" and not restored.called
        assert "RESTORE the snapshot /BTF_CONF/Quadrupoles / tuesday" in first["action"]
        assert "1 setpoint PV(s) WOULD CHANGE (live -> saved): A:SP: 5.0 -> 4.2 (delta -0.8)" in first["action"]
        assert "1 already match" in first["action"]
        done = json.loads((await registry.dispatch(
            "restore_saverestore_snapshot", {**args, "confirmation_id": first["confirmation_id"]}, ctx
        ))[0].text)

    assert restored.calls.last.request.url.params["nodeId"] == SNAP_NEW
    assert restored.calls.last.request.headers["authorization"] == "Bearer eyJuser"
    assert done["status"] == "success" and done["pvs_written"] == 1
    assert done["failed"] == [{"pv_name": "A:SP", "error": "Failed to write to PV 'A:SP'"}]
    assert "1 PV(s) FAILED: A:SP" in done["logbook_suggestion"]["text"]
    assert "ASK whether to record it in the logbook" in done["next_step"]


@respx.mock
async def test_restoring_a_snapshot_that_matches_the_machine_says_nothing_would_change(app_context: AppContext):
    _mock_snapshots()
    respx.get(f"{SAR}/node/{SNAP_OLD}").mock(return_value=httpx.Response(200, json=_node(SNAP_OLD, "monday", "SNAPSHOT")))
    respx.get(f"{SAR}/compare/{SNAP_OLD}").mock(
        return_value=httpx.Response(200, json=[{"pvName": "A:SP", "equal": True, "storedValue": {"value": 1}, "liveValue": {"value": 1}}])
    )
    first = json.loads((await build_registry().dispatch(
        "restore_saverestore_snapshot", {"snapshot": SNAP_OLD}, _ctx(app_context)
    ))[0].text)
    assert first["status"] == "confirmation_required"
    assert "all 1 PV(s) already match the snapshot, so no value would change" in first["action"]
