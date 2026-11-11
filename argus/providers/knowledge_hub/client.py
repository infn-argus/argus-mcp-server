"""MCP client for the ARGUS Knowledge Hub.

The hub (argus-knowledge-hub) serves its equipment, tickets, documents and
logbook as an MCP server over streamable HTTP, behind the same personal
access token as its REST API. ARGUS talks to it as an MCP *client* -- the
same protocol LibreChat uses -- rather than through the hub's REST API, so
it gets exactly the records, links and restricted-record redaction the
hub's tools give any other assistant.

Plain JSON-RPC over httpx instead of the mcp SDK's ClientSession: that
session is an async context manager meant to live as long as a
conversation, which doesn't fit a provider answering independent tool calls
from many conversations. The protocol subset needed here is small: the
initialize handshake (once, reusing a Mcp-Session-Id if the server issues
one), tools/call, and a response that is either JSON or an SSE stream.

Every hub tool works in one workspace and takes an optional `workspace`
argument; list_workspaces says which ones the token may ask in. Callers fan
out across workspace_ids() -- the configured KNOWLEDGE_HUB_WORKSPACES, or
every workspace the token may read when that's blank (all of them, for an
administrator's token with the `admin` scope).

With KNOWLEDGE_HUB_AS_CALLER the credential is the caller's own access token
(core/request_context.current_caller_token), so the hub applies that person's
access. Each credential has its own MCP session and its own workspace list:
nothing one person's token was answered is reused for another.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, TypeVar

import httpx

from argus.config.settings import KnowledgeHubSettings
from argus.core.cache import AsyncTTLCache
from argus.core.request_context import current_caller_token
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.knowledge_hub.exceptions import (
    KnowledgeHubAuthError,
    KnowledgeHubQueryError,
    KnowledgeHubTimeoutError,
    KnowledgeHubUnavailableError,
    KnowledgeHubUnconfiguredError,
)
from argus.providers.knowledge_hub.models import HubWorkspace

T = TypeVar("T")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "argus-mcp-server", "version": "1"}
# Sessions kept at once, one per credential: the least recently used is dropped (and re-initialized if needed).
MAX_SESSIONS = 256


@dataclass
class _Session:
    """One credential's MCP session with the hub."""

    token: str
    session_id: str | None = None
    initialized: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class KnowledgeHubClient:
    name: ClassVar[str] = "knowledge_hub"

    def __init__(self, settings: KnowledgeHubSettings) -> None:
        self._settings = settings
        self._ids = itertools.count(1)
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        # The workspace listing changes on the order of days; one GET per
        # five minutes at most per credential, shared by every concurrent fan-out.
        self._workspace_cache: AsyncTTLCache = AsyncTTLCache(maxsize=MAX_SESSIONS, ttl=300.0)

    def is_configured(self) -> bool:
        return bool(self._settings.url and (self._settings.token or self._settings.as_caller))

    def _session(self) -> tuple[str, _Session]:
        """The credential for this call and its session: the caller's token with KNOWLEDGE_HUB_AS_CALLER, else
        the service token. Keyed by a digest, so no token is kept as a key or written anywhere."""
        if self._settings.as_caller:
            token = current_caller_token()
            if not token:
                raise KnowledgeHubAuthError(
                    "The Knowledge Hub is asked as the person calling, and this call carries no sign-in "
                    "(X-Argus-User-Token): sign in to the assistant again."
                )
        else:
            token = self._settings.token or ""
        key = hashlib.sha256(token.encode()).hexdigest()[:24]
        session = self._sessions.get(key)
        if session is None:
            session = self._sessions[key] = _Session(token=token)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        else:
            session.token = token
            self._sessions.move_to_end(key)
        return key, session

    def configured_workspaces(self) -> list[str]:
        return [w.strip() for w in (self._settings.workspaces or "").split(",") if w.strip()]

    @property
    def timeout(self) -> float:
        return self._settings.timeout_seconds

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise KnowledgeHubUnconfiguredError(
                "The ARGUS Knowledge Hub is not configured (set KNOWLEDGE_HUB_URL and KNOWLEDGE_HUB_TOKEN)."
            )

    def _headers(self, session: _Session) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {session.token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        # The request's own workspace, for a token that isn't fixed to one.
        # Every tool call names its workspace explicitly anyway; this only
        # matters to a hub that still insists on the header.
        configured = self.configured_workspaces()
        if configured:
            headers["X-Workspace-Id"] = configured[0]
        if session.session_id:
            headers["Mcp-Session-Id"] = session.session_id
        return headers

    async def health(self) -> ProviderHealth:
        if not self.is_configured():
            return ProviderHealth(name=self.name, status=ProviderStatus.UNCONFIGURED)
        try:
            if self._settings.as_caller and not current_caller_token():
                # A health probe has no caller: say the hub is configured to be asked as one, not that it is down.
                return ProviderHealth(name=self.name, status=ProviderStatus.OK, detail="asked as each caller")
            await self._rpc("ping", {}, timeout=3)
        except (KnowledgeHubUnavailableError, KnowledgeHubTimeoutError, KnowledgeHubAuthError) as exc:
            return ProviderHealth(name=self.name, status=ProviderStatus.UNAVAILABLE, detail=str(exc))
        except KnowledgeHubQueryError:
            pass  # it answered; a server without ping is still up
        return ProviderHealth(name=self.name, status=ProviderStatus.OK)

    async def _post(self, session: _Session, payload: dict, *, timeout: float) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.post(self._settings.url, json=payload, headers=self._headers(session))
        except httpx.TimeoutException as exc:
            raise KnowledgeHubTimeoutError(f"Timeout calling the Knowledge Hub ({payload.get('method')})") from exc
        except httpx.HTTPError as exc:
            raise KnowledgeHubUnavailableError(f"Knowledge Hub unreachable: {exc}") from exc

    def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise KnowledgeHubAuthError(
                f"The Knowledge Hub refused the token (HTTP {resp.status_code}): {_detail(resp)}"
            )
        if resp.status_code >= 400:
            raise KnowledgeHubUnavailableError(f"Knowledge Hub returned HTTP {resp.status_code}: {_detail(resp)}")

    async def _send(self, session: _Session, method: str, params: dict, *, timeout: float) -> dict:
        request_id = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        resp = await self._post(session, payload, timeout=timeout)
        if resp.status_code == 404 and session.session_id:
            # The server forgot our session (restart, expiry): the spec's
            # answer is 404, and the client's is to initialize again.
            session.session_id = None
            session.initialized = False
            raise _SessionExpired()
        self._check_status(resp)
        if session_id := resp.headers.get("mcp-session-id"):
            session.session_id = session_id
        message = _parse_message(resp, request_id)
        if "error" in message:
            error = message["error"] or {}
            raise KnowledgeHubQueryError(f"Knowledge Hub error {error.get('code')}: {error.get('message')}")
        return message.get("result") or {}

    async def _ensure_initialized(self, session: _Session, timeout: float) -> None:
        if session.initialized:
            return
        async with session.lock:
            if session.initialized:
                return
            await self._send(
                session,
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
                timeout=timeout,
            )
            notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            resp = await self._post(session, notification, timeout=timeout)
            self._check_status(resp)
            session.initialized = True

    async def _rpc(self, method: str, params: dict, *, timeout: float | None = None) -> dict:
        self._require_configured()
        timeout = timeout if timeout is not None else self.timeout
        _, session = self._session()
        for attempt in range(2):
            await self._ensure_initialized(session, timeout)
            try:
                return await self._send(session, method, params, timeout=timeout)
            except _SessionExpired:
                if attempt:
                    raise KnowledgeHubUnavailableError("The Knowledge Hub keeps dropping the MCP session.") from None
        raise AssertionError("unreachable")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        workspace: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call one hub tool and return its result, JSON-decoded when it is JSON (every hub tool's is)."""
        args = dict(arguments or {})
        if workspace:
            args["workspace"] = workspace
        result = await self._rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        text = "".join(
            part.get("text", "") for part in result.get("content") or [] if isinstance(part, dict)
        )
        if result.get("isError"):
            where = f" in workspace {workspace!r}" if workspace else ""
            raise KnowledgeHubQueryError(f"Knowledge Hub tool {name}{where} failed: {text}")
        try:
            return json.loads(text)
        except ValueError:
            return text

    async def _fetch_workspaces(self) -> list[HubWorkspace]:
        payload = await self.call_tool("list_workspaces")
        try:
            return [
                HubWorkspace(id=str(w["id"]), name=str(w.get("name") or w["id"]), shared=bool(w.get("shared")))
                for w in payload["workspaces"]
            ]
        except (KeyError, TypeError) as exc:
            raise KnowledgeHubQueryError(f"Unexpected list_workspaces response shape: {exc}") from exc

    async def list_workspaces(self) -> list[HubWorkspace]:
        """The workspaces ARGUS asks in: the configured ones that the token may read, or all it may read."""
        self._require_configured()
        key, _ = self._session()
        # Per credential: one person's readable workspaces are never another's.
        available = await self._workspace_cache.get_or_set(f"workspaces:{key}", self._fetch_workspaces)
        configured = self.configured_workspaces()
        if not configured:
            return list(available)
        by_id = {w.id: w for w in available}
        return [by_id[w] for w in configured if w in by_id]

    async def workspace_ids(self, only: str | None = None) -> list[str]:
        """The workspaces to fan a search out across, or just `only` (checked against what the token may read)."""
        workspaces = await self.list_workspaces()
        ids = [w.id for w in workspaces]
        if only:
            if only not in ids:
                raise KnowledgeHubQueryError(
                    f"Workspace {only!r} is not one this server asks in; known workspaces: {', '.join(ids) or 'none'}."
                )
            return [only]
        if not ids:
            configured = self.configured_workspaces()
            hint = (
                f"none of KNOWLEDGE_HUB_WORKSPACES ({', '.join(configured)}) is readable with this token"
                if configured
                else "the token may read no workspace"
            )
            raise KnowledgeHubQueryError(f"No Knowledge Hub workspace to ask in: {hint}.")
        return ids

    async def for_each_workspace(
        self, workspaces: list[str], fn: Callable[[str], Awaitable[T]]
    ) -> tuple[dict[str, T], dict[str, str]]:
        """Run fn once per workspace concurrently. Returns (results, errors) keyed by workspace id.

        One workspace failing (a restricted one, a hub-side tool error) degrades
        the answer rather than failing it -- unless every workspace failed, in
        which case the first error is re-raised.
        """
        outcomes = await asyncio.gather(*(fn(ws) for ws in workspaces), return_exceptions=True)
        results: dict[str, T] = {}
        errors: dict[str, str] = {}
        first_error: BaseException | None = None
        for ws, outcome in zip(workspaces, outcomes):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                errors[ws] = str(outcome)
                first_error = first_error or outcome
            else:
                results[ws] = outcome
        if not results and first_error is not None:
            raise first_error
        return results, errors


class _SessionExpired(Exception):
    pass


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("message") or body)[:200]
    return str(body)[:200]


def _parse_message(resp: httpx.Response, request_id: int) -> dict:
    """The JSON-RPC response to request_id, from a JSON body or an SSE stream."""
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        for message in _sse_messages(resp.text):
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        raise KnowledgeHubQueryError("The Knowledge Hub's event stream carried no response to the request.")
    try:
        message = resp.json()
    except ValueError as exc:
        raise KnowledgeHubQueryError(f"The Knowledge Hub answered with non-JSON: {resp.text[:200]}") from exc
    if not isinstance(message, dict):
        raise KnowledgeHubQueryError("The Knowledge Hub answered with an unexpected JSON-RPC shape.")
    return message


def _sse_messages(text: str) -> list[Any]:
    messages: list[Any] = []
    data: list[str] = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            try:
                messages.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return messages
