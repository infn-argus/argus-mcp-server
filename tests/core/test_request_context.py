import structlog.contextvars

import asyncio

from argus.core.request_context import caller_token_scope, current_caller_token, request_scope


async def test_request_scope_binds_request_id_only_by_default():
    with request_scope() as rid:
        bound = structlog.contextvars.get_contextvars()
        assert bound["request_id"] == rid
        assert "conversation_id" not in bound


async def test_request_scope_binds_extra_fields():
    with request_scope(extra={"conversation_id": "conv-1", "user_id": "user-1"}):
        bound = structlog.contextvars.get_contextvars()
        assert bound["conversation_id"] == "conv-1"
        assert bound["user_id"] == "user-1"
        assert "request_id" in bound


async def test_request_scope_unbinds_everything_on_exit():
    with request_scope(extra={"conversation_id": "conv-1"}):
        pass
    bound = structlog.contextvars.get_contextvars()
    assert "request_id" not in bound
    assert "conversation_id" not in bound


async def test_request_scope_with_no_extra_does_not_error():
    with request_scope(extra=None):
        bound = structlog.contextvars.get_contextvars()
        assert "request_id" in bound


async def test_caller_token_scope_sets_and_restores_the_token():
    assert current_caller_token() is None
    with caller_token_scope("eyJabc"):
        assert current_caller_token() == "eyJabc"
    assert current_caller_token() is None
    with caller_token_scope(None):
        assert current_caller_token() is None


async def test_caller_token_reaches_tasks_created_in_the_scope_and_never_structlog():
    # Providers run as asyncio tasks (gather_with_timeout); they copy the context.
    with request_scope(), caller_token_scope("eyJabc"):
        assert await asyncio.create_task(_read_token()) == "eyJabc"
        assert "eyJabc" not in structlog.contextvars.get_contextvars().values()


async def _read_token():
    return current_caller_token()
