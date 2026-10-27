"""Request-id plumbing threaded through every MCP tool call via contextvars."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# The identity-provider access token of the person behind the current tool
# call, when their client forwarded one (see mcp_server/correlation.py's
# extract_caller_token). Deliberately separate from request_scope: it is a
# credential, so it is never bound to structlog's contextvars and never
# appears in a log line. Providers that can act as the caller (the Olog
# logbook) read it with current_caller_token().
_caller_token_var: ContextVar[str | None] = ContextVar("caller_token", default=None)

# Whether the client behind the current call wants write tools confirmed in
# two steps (see mcp_server/confirmation.py). Set per call from the
# X-Argus-Confirm-Writes header - LibreChat's registrations send it, the
# voice agent (which has its own operator confirmation) doesn't.
_confirm_writes_var: ContextVar[bool] = ContextVar("confirm_writes", default=False)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def current_caller_token() -> str | None:
    """The calling person's access token for this tool call, or None."""
    return _caller_token_var.get()


@contextmanager
def caller_token_scope(token: str | None) -> Iterator[None]:
    """Make ``token`` the caller's token for everything run inside this scope,
    including asyncio tasks created in it (they copy the context)."""
    reset = _caller_token_var.set(token or None)
    try:
        yield
    finally:
        _caller_token_var.reset(reset)


def writes_need_confirmation() -> bool:
    """Whether write tools must be confirmed in two steps for this call."""
    return _confirm_writes_var.get()


@contextmanager
def confirm_writes_scope(enabled: bool) -> Iterator[None]:
    reset = _confirm_writes_var.set(bool(enabled))
    try:
        yield
    finally:
        _confirm_writes_var.reset(reset)


@contextmanager
def request_scope(request_id: str | None = None, extra: dict[str, str] | None = None) -> Iterator[str]:
    """Bind a request id (and optional extra fields) to the current context and
    to structlog's contextvars.

    Every log line emitted while inside this scope (including provider_call
    events logged deep in gather_with_timeout) automatically carries request_id
    and whatever ``extra`` was passed — e.g. LibreChat's conversation_id/
    message_id/user_id, when the MCP call carried them as headers (see
    mcp_server/server.py's handle_call_tool), so a Loki query for one bad-rated
    conversation shows every tool-call log line it triggered.
    """
    rid = request_id or new_request_id()
    token = request_id_var.set(rid)
    bound = {"request_id": rid, **(extra or {})}
    structlog.contextvars.bind_contextvars(**bound)
    try:
        yield rid
    finally:
        request_id_var.reset(token)
        structlog.contextvars.unbind_contextvars(*bound.keys())
