from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
import respx

from argus.config.settings import LogbookSettings
from argus.core.request_context import caller_token_scope
from argus.mcp_server.confirmation import caller_key
from argus.providers.logbook.exceptions import LogbookRefusedError, LogbookSignInError
from argus.providers.logbook.logbook import LogbookProvider

OLOG = "http://olog.test"


def _jwt(exp: float, sub: str = "person-1") -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'iss': 'https://idp', 'sub': sub, 'exp': exp})}.sig"


def _provider(**kw) -> LogbookProvider:
    return LogbookProvider(LogbookSettings(_env_file=None, LOGBOOK_BASE_URL=OLOG, **kw))


def _logbooks():
    respx.get(f"{OLOG}/Olog/logbooks").mock(return_value=httpx.Response(
        200, json=[{"name": "TEST"}, {"name": "controls"}, {"name": "operations"}]))


@respx.mock
async def test_logbook_name_is_matched_ignoring_case():
    _logbooks()
    put = respx.put(f"{OLOG}/Olog/logs").mock(return_value=httpx.Response(200, json={"id": 7, "title": "t"}))
    with caller_token_scope(_jwt(time.time() + 300)):
        await _provider().create_entry("t", "x", ["Operations"])
    assert json.loads(put.calls[0].request.content)["logbooks"] == [{"name": "operations"}]


@respx.mock
async def test_unknown_logbook_is_refused_before_posting():
    _logbooks()
    put = respx.put(f"{OLOG}/Olog/logs")
    with caller_token_scope(_jwt(time.time() + 300)), pytest.raises(LogbookRefusedError, match="TEST, controls, operations"):
        await _provider().create_entry("t", "x", ["shift"])
    assert not put.called


@respx.mock
async def test_expired_or_missing_sign_in_stops_the_write():
    put = respx.put(f"{OLOG}/Olog/logs")
    with caller_token_scope(_jwt(time.time() - 1)), pytest.raises(LogbookSignInError, match="new message"):
        await _provider().create_entry("t", "x", ["operations"])
    with pytest.raises(LogbookSignInError):
        await _provider().create_entry("t", "x", ["operations"])
    assert not put.called


def test_confirmation_caller_survives_a_token_refresh():
    old, new = _jwt(time.time() + 10), _jwt(time.time() + 300)
    assert old != new and caller_key(old, None) == caller_key(new, None)
    assert caller_key(old, None) != caller_key(_jwt(time.time() + 10, sub="someone-else"), None)
    assert caller_key(old, {"user_id": "u1"}) == caller_key(None, {"user_id": "u1"})


def test_a_confirmation_without_any_sign_in_says_the_sign_in_expired():
    from argus.mcp_server.confirmation import ConfirmationStore

    store = ConfirmationStore()
    issued = store.issue("create_logbook_entry", {"title": "t"}, caller_key(_jwt(time.time() + 30), None))
    problem = store.redeem(issued, "create_logbook_entry", {"title": "t"}, caller_key(None, None))
    assert problem and "expired" in problem and "another user" not in problem
