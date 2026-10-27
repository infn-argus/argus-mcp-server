"""Two-step writes for clients that ask for them (X-Argus-Confirm-Writes), end to end through the registry."""

import json

import httpx
import respx

from argus.config.settings import LogbookSettings
from argus.core.context import AppContext
from argus.core.request_context import caller_token_scope, confirm_writes_scope
from argus.mcp_server.server import build_registry
from argus.providers.logbook.logbook import LogbookProvider
from argus.services.history_service import HistoryService

WRITE_TOOLS = {"set_pv", "set_pv_value", "restart_ioc", "execute_procedure", "create_logbook_entry", "update_logbook_entry"}


def _with_olog(app_context: AppContext) -> AppContext:
    logbook = LogbookProvider(LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test"))
    history = HistoryService(archiver=app_context.archiver, logbook=logbook, elastic=app_context.elastic, loki=app_context.loki)
    return AppContext(**{**app_context.__dict__, "logbook": logbook, "history_service": history})


def _payload(response) -> dict:
    return json.loads(response[0].text)


def test_only_write_tools_gain_confirmation_and_only_for_confirming_clients():
    registry = build_registry()
    plain = {t.name: t for t in registry.list_tools()}
    confirming = {t.name: t for t in registry.list_tools(confirm_writes=True)}

    assert all("confirmation_id" not in t.inputSchema["properties"] for t in plain.values())
    with_confirmation = {n for n, t in confirming.items() if "confirmation_id" in t.inputSchema["properties"]}
    assert with_confirmation == WRITE_TOOLS
    assert "two steps" in confirming["create_logbook_entry"].description
    # Read tools are identical either way.
    assert confirming["get_pv"] == plain["get_pv"]


async def test_without_the_header_writes_run_in_one_call_as_before(app_context: AppContext):
    registry = build_registry()
    payload = _payload(await registry.dispatch("set_pv", {"pv_name": "QF12:CURRENT", "value": 5.0}, app_context))
    assert payload["status"] == "success"
    assert app_context.epics_ca.values["QF12:CURRENT"] == 5.0


async def test_a_confirming_client_writes_only_on_the_confirmed_second_call(app_context: AppContext):
    registry = build_registry()
    args = {"pv_name": "QF12:CURRENT", "value": 7.5}
    with confirm_writes_scope(True):
        first = _payload(await registry.dispatch("set_pv", dict(args), app_context))
        assert first["status"] == "confirmation_required"
        assert first["action"] == "Write 7.5 to PV QF12:CURRENT (a live change to the machine)."
        assert app_context.epics_ca.values["QF12:CURRENT"] == 4.2  # nothing written

        second = _payload(
            await registry.dispatch("set_pv", {**args, "confirmation_id": first["confirmation_id"]}, app_context)
        )
        assert second["status"] == "success"
        assert app_context.epics_ca.values["QF12:CURRENT"] == 7.5

        again = _payload(
            await registry.dispatch("set_pv", {**args, "confirmation_id": first["confirmation_id"]}, app_context)
        )
        assert again["status"] == "error" and again["code"] == "confirmation_invalid"


async def test_a_confirmation_cannot_be_reused_for_different_arguments(app_context: AppContext):
    registry = build_registry()
    with confirm_writes_scope(True):
        first = _payload(await registry.dispatch("set_pv", {"pv_name": "QF12:CURRENT", "value": 1.0}, app_context))
        swapped = _payload(
            await registry.dispatch(
                "set_pv", {"pv_name": "QF12:CURRENT", "value": 99.0, "confirmation_id": first["confirmation_id"]}, app_context
            )
        )
    assert swapped["code"] == "confirmation_invalid"
    assert app_context.epics_ca.values["QF12:CURRENT"] == 4.2


async def test_read_tools_never_ask_for_confirmation(app_context: AppContext):
    with confirm_writes_scope(True):
        payload = _payload(await build_registry().dispatch("get_pv", {"pv_name": "QF12:CURRENT"}, app_context))
    assert payload["status"] == "success"


@respx.mock
async def test_a_confirmed_logbook_entry_is_posted_once_as_the_signed_in_user(app_context: AppContext):
    ctx = _with_olog(app_context)
    route = respx.put("http://olog.test/Olog/logs").mock(
        return_value=httpx.Response(200, json={"id": 9, "title": "Beam lost", "description": "x", "owner": "jdoe"})
    )
    args = {"title": "Beam lost", "text": "x", "logbooks": ["Operations"]}
    registry = build_registry()
    with confirm_writes_scope(True), caller_token_scope("eyJuser"):
        first = _payload(await registry.dispatch("create_logbook_entry", dict(args), ctx))
        assert first["status"] == "confirmation_required" and not route.called
        assert "NEW entry to the Operations logbook" in first["action"]
        done = _payload(
            await registry.dispatch("create_logbook_entry", {**args, "confirmation_id": first["confirmation_id"]}, ctx)
        )
    assert done["status"] == "success" and done["author"] == "jdoe"
    assert route.call_count == 1
    assert route.calls.last.request.headers["authorization"] == "Bearer eyJuser"


@respx.mock
async def test_another_user_cannot_use_someone_elses_confirmation(app_context: AppContext):
    ctx = _with_olog(app_context)
    route = respx.put("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json={"id": 9}))
    args = {"title": "t", "text": "x", "logbooks": ["Operations"]}
    registry = build_registry()
    with confirm_writes_scope(True):
        with caller_token_scope("eyJalice"):
            first = _payload(await registry.dispatch("create_logbook_entry", dict(args), ctx))
        with caller_token_scope("eyJbob"):
            stolen = _payload(
                await registry.dispatch("create_logbook_entry", {**args, "confirmation_id": first["confirmation_id"]}, ctx)
            )
    assert stolen["code"] == "confirmation_invalid" and not route.called
