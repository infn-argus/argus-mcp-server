"""Per-tool-call correlation: extracts LibreChat's conversation/message/user
IDs from the MCP request's HTTP headers, when present, so argus-mcp-server's
own structured logs (see core/request_context.py) can be tied back to the
exact LibreChat conversation/message that triggered them.

LibreChat's MCP client refreshes these headers before every individual tool
call (not just once per SSE connection), and the mcp SDK captures the raw
HTTP request per JSON-RPC message and exposes it via Server.request_context
inside a @server.call_tool() handler — see argus-helm-chart's
librechat-configmap.yaml for where the headers themselves get set.

Only SSE/streamable-http transports carry a real HTTP request per call;
stdio transport (and any call made without these headers, e.g. a non-
LibreChat MCP client) yields an empty dict, which is a no-op for
request_scope().
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server

_HEADER_TO_FIELD = {
    "x-librechat-conversation-id": "conversation_id",
    "x-librechat-message-id": "message_id",
    "x-librechat-user-id": "user_id",
}


# The calling person's identity-provider access token, forwarded by their
# client so that providers able to act as that person (the Olog logbook) do.
# LibreChat fills it per tool call from a header template such as
#   X-Argus-User-Token: "{{LIBRECHAT_OPENID_ACCESS_TOKEN}}"
# (epik8s-platform: argusMcpInstances[].forwardUserToken). A dedicated header,
# not Authorization: ARGUS itself doesn't authenticate MCP callers - this is a
# credential to pass on, not one to check.
CALLER_TOKEN_HEADER = "x-argus-user-token"


# A client asking for write tools to be confirmed in two steps
# (mcp_server/confirmation.py). Static per registration, set by whoever
# configured the client - LibreChat's mcpServers entries in epik8s-platform -
# so the model can't turn it off. "true"/"1"/"yes" enable it.
CONFIRM_WRITES_HEADER = "x-argus-confirm-writes"


def current_http_request(server: Server) -> Any | None:
    """The raw Starlette Request for the MCP call currently being handled, or
    None outside of an HTTP-transported call (stdio, or outside a request
    entirely)."""
    try:
        return server.request_context.request
    except LookupError:
        return None


def extract_correlation_headers(request: Any | None) -> dict[str, str]:
    if request is None:
        return {}
    headers = getattr(request, "headers", None)
    if headers is None:
        return {}
    result: dict[str, str] = {}
    for header_name, field in _HEADER_TO_FIELD.items():
        value = headers.get(header_name)
        if value:
            result[field] = value
    return result


def extract_caller_token(request: Any | None) -> str | None:
    """The caller's forwarded access token, or None.

    LibreChat leaves a header template as-is when it has nothing to put in it
    (e.g. a user who didn't sign in with OpenID, or OPENID_REUSE_TOKENS off),
    so an unresolved "{{...}}" placeholder counts as no token, as does an
    empty value. A leading "Bearer " is accepted and dropped.
    """
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    value = (headers.get(CALLER_TOKEN_HEADER) or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or "{{" in value:
        return None
    return value


def extract_confirm_writes(request: Any | None) -> bool:
    headers = getattr(request, "headers", None) if request is not None else None
    if headers is None:
        return False
    return (headers.get(CONFIRM_WRITES_HEADER) or "").strip().lower() in ("true", "1", "yes")
