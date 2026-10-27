"""A tool call carrying the caller's token reaches Olog as that person, end to end through the registry."""

import json

import httpx
import respx

from argus.config.settings import LogbookSettings
from argus.core.context import AppContext
from argus.core.request_context import caller_token_scope
from argus.mcp_server.server import build_registry
from argus.providers.logbook.logbook import LogbookProvider
from argus.services.history_service import HistoryService


@respx.mock
async def test_get_alarm_history_reads_olog_with_the_callers_token(app_context: AppContext):
    logbook = LogbookProvider(LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test"))
    ctx = AppContext(
        **{
            **app_context.__dict__,
            "logbook": logbook,
            "history_service": HistoryService(
                archiver=app_context.archiver, logbook=logbook, elastic=app_context.elastic, loki=app_context.loki
            ),
        }
    )
    route = respx.get("http://olog.test/Olog/logs").mock(return_value=httpx.Response(200, json=[]))

    with caller_token_scope("eyJuser"):
        response = await build_registry().dispatch("get_alarm_history", {"device_name": "QUATM002"}, ctx)

    assert json.loads(response[0].text)["status"] == "success"
    assert route.calls.last.request.headers["authorization"] == "Bearer eyJuser"



@respx.mock
async def test_a_refused_entry_reaches_the_model_as_a_clear_logbook_refused_error(app_context: AppContext):
    logbook = LogbookProvider(LogbookSettings(_env_file=None, LOGBOOK_BASE_URL="http://olog.test"))
    ctx = AppContext(
        **{
            **app_context.__dict__,
            "logbook": logbook,
            "history_service": HistoryService(
                archiver=app_context.archiver, logbook=logbook, elastic=app_context.elastic, loki=app_context.loki
            ),
        }
    )
    respx.put("http://olog.test/Olog/logs").mock(
        return_value=httpx.Response(400, json={"message": "One or more invalid logbook name(s)"})
    )
    respx.get("http://olog.test/Olog/logbooks").mock(return_value=httpx.Response(200, json=[{"name": "SPARC-Shift"}]))

    payload = json.loads(
        (await build_registry().dispatch(
            "create_logbook_entry", {"title": "t", "text": "x", "logbooks": ["Operations"]}, ctx
        ))[0].text
    )
    assert payload["status"] == "error" and payload["code"] == "logbook_refused"
    assert "Valid logbooks here: SPARC-Shift." in payload["message"]
