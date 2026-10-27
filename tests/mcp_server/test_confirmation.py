import time

from argus.mcp_server.confirmation import ConfirmationStore, arguments_digest, caller_key


def test_an_id_confirms_exactly_that_call_once():
    store = ConfirmationStore()
    cid = store.issue("set_pv", {"pv_name": "A", "value": 1}, "token:x")
    assert store.redeem(cid, "set_pv", {"pv_name": "A", "value": 1}, "token:x") is None
    assert "unknown or expired" in store.redeem(cid, "set_pv", {"pv_name": "A", "value": 1}, "token:x")


def test_other_arguments_another_tool_or_another_caller_are_refused():
    store = ConfirmationStore()
    cid = store.issue("set_pv", {"pv_name": "A", "value": 1}, "token:x")
    assert "arguments differ" in store.redeem(cid, "set_pv", {"pv_name": "A", "value": 2}, "token:x")
    cid = store.issue("set_pv", {"pv_name": "A", "value": 1}, "token:x")
    assert "arguments differ" in store.redeem(cid, "restart_ioc", {"pv_name": "A", "value": 1}, "token:x")
    cid = store.issue("set_pv", {"pv_name": "A", "value": 1}, "token:x")
    assert "another user" in store.redeem(cid, "set_pv", {"pv_name": "A", "value": 1}, "token:y")


def test_an_id_expires(monkeypatch):
    store = ConfirmationStore(ttl=10)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    cid = store.issue("set_pv", {"pv_name": "A"}, "anonymous")
    monkeypatch.setattr(time, "monotonic", lambda: now + 11)
    assert "unknown or expired" in store.redeem(cid, "set_pv", {"pv_name": "A"}, "anonymous")


def test_digest_ignores_key_order_and_the_confirmation_itself():
    assert arguments_digest("t", {"a": 1, "b": [1, 2]}) == arguments_digest("t", {"b": [1, 2], "a": 1, "confirmation_id": "c"})


def test_caller_key_prefers_the_token_and_never_contains_it():
    key = caller_key("eyJsecret", {"user_id": "u1"})
    assert key.startswith("token:") and "eyJsecret" not in key
    assert caller_key(None, {"user_id": "u1"}) == "user:u1"
    assert caller_key(None, None) == "anonymous"
