"""Tool registration and the single error->structured-response dispatch boundary.

MCP tools orchestrate services; they never contain business logic themselves.
Every handler is `async def handler(arguments: dict, ctx: AppContext) -> dict`
returning the fields to merge into `{"status": "success", ...}` — the registry
takes care of JSON encoding, exceptions, and request-id/logging scope.
"""

from __future__ import annotations

import json
import math
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from mcp.types import TextContent, Tool

from argus.core.context import AppContext
from argus.core.errors import ArgusError
from argus.core.request_context import current_caller_token, request_scope, writes_need_confirmation
from argus.mcp_server.confirmation import CONFIRMATION_ARG, ConfirmationStore, caller_key

logger = structlog.get_logger(__name__)

ToolHandler = Callable[[dict[str, Any], AppContext], Awaitable[dict[str, Any]]]

_CONFIRM_NOTE = (
    " This tool WRITES. It runs in two steps: call it first WITHOUT confirmation_id - nothing is changed, "
    "and you get a preview and a confirmation_id. Show the user the preview in plain words and ask them "
    "to confirm. Only after the user explicitly says yes, call it again with exactly the same arguments "
    "plus that confirmation_id. Never confirm on the user's behalf, and never call it to read anything."
)
_CONFIRM_PROPERTY = {
    "type": "string",
    "description": "Only on the second call, after the user explicitly confirmed the preview: the "
    "confirmation_id the first call returned.",
}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    # State-changing (a PV write, a pod restart, a logbook entry...): subject
    # to two-step confirmation for clients that ask for it (confirmation.py).
    writes: bool = False
    # Plain-language preview of what a call would do, shown to the user
    # before they confirm; defaults to the tool name and its arguments. It may
    # take (arguments, ctx) and be async, to show what will really happen
    # (e.g. the PVs a configuration will get) - and an ArgusError it raises
    # stops the call before any confirmation is asked for.
    describe: Callable[..., str | Awaitable[str]] | None = None
    # Confirm in two steps for EVERY client, not only those asking for it -
    # for writes whose blast radius is the whole machine (restoring a
    # snapshot), where a client without its own gate must not get one-shot.
    always_confirm: bool = False

    def needs_confirmation(self, client_confirms_writes: bool) -> bool:
        return self.writes and (client_confirms_writes or self.always_confirm)

    def as_mcp_tool(self, confirm_writes: bool = False) -> Tool:
        if not self.needs_confirmation(confirm_writes):
            return Tool(name=self.name, description=self.description, inputSchema=self.input_schema)
        schema = dict(self.input_schema)
        schema["properties"] = {**schema.get("properties", {}), CONFIRMATION_ARG: _CONFIRM_PROPERTY}
        return Tool(name=self.name, description=self.description + _CONFIRM_NOTE, inputSchema=schema)

    async def preview(self, arguments: dict[str, Any], ctx: AppContext) -> str:
        if self.describe is None:
            shown = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
            return f"{self.name}({shown})"
        takes_ctx = len(inspect.signature(self.describe).parameters) > 1
        result = self.describe(arguments, ctx) if takes_ctx else self.describe(arguments)
        return await result if inspect.isawaitable(result) else result


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.confirmations = ConfirmationStore()

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def register_many(self, tools: list[ToolDefinition]) -> None:
        for tool in tools:
            self.register(tool)

    def list_tools(self, confirm_writes: bool = False) -> list[Tool]:
        """The tools; write tools gain the confirmation_id argument when the client confirms writes."""
        return [tool.as_mcp_tool(confirm_writes) for tool in self._tools.values()]

    async def dispatch(
        self,
        name: str,
        arguments: dict[str, Any],
        ctx: AppContext,
        *,
        correlation: dict[str, str] | None = None,
    ) -> list[TextContent]:
        tool = self._tools.get(name)
        if tool is None:
            return [_error_response("unknown_tool", f"Unknown tool: {name}")]

        with request_scope(extra=correlation):
            structlog.contextvars.bind_contextvars(tool=name)
            try:
                if tool.needs_confirmation(writes_need_confirmation()):
                    gate = await self._confirmation_gate(tool, arguments, ctx, correlation)
                    if gate is not None:
                        return [gate]
                    arguments = {k: v for k, v in arguments.items() if k != CONFIRMATION_ARG}
                result = await tool.handler(arguments, ctx)
                return [_success_response(result)]
            except ArgusError as exc:
                logger.warning("tool_error", code=exc.code, error=str(exc))
                return [_error_response(exc.code, str(exc))]
            except NotImplementedError as exc:
                logger.info("tool_not_implemented", error=str(exc))
                return [_error_response("not_implemented", str(exc))]
            except Exception:
                logger.exception("tool_unexpected_error")
                return [_error_response("internal_error", "An internal error occurred.")]
            finally:
                structlog.contextvars.unbind_contextvars("tool")


    async def _confirmation_gate(
        self, tool: ToolDefinition, arguments: dict[str, Any], ctx: AppContext, correlation: dict[str, str] | None
    ) -> TextContent | None:
        """None when this call carries a valid confirmation; otherwise the response that stands in for it."""
        caller = caller_key(current_caller_token(), correlation)
        confirmation_id = arguments.get(CONFIRMATION_ARG)
        call_args = {k: v for k, v in arguments.items() if k != CONFIRMATION_ARG}
        if not confirmation_id:
            action = await tool.preview(call_args, ctx)  # may raise: nothing to confirm then
            issued = self.confirmations.issue(tool.name, call_args, caller)
            logger.info("write_confirmation_requested")
            return _json_response(
                {
                    "status": "confirmation_required",
                    "action": action,
                    "tool": tool.name,
                    "arguments": call_args,
                    CONFIRMATION_ARG: issued,
                    "expires_in_seconds": int(self.confirmations.ttl),
                    "next_step": (
                        "Nothing has been changed. Show the user the action above and ask them to confirm. "
                        f"Only if they explicitly agree, call {tool.name} again with exactly the same "
                        f"arguments plus {CONFIRMATION_ARG}=\"{issued}\"."
                    ),
                }
            )
        problem = self.confirmations.redeem(str(confirmation_id), tool.name, call_args, caller)
        if problem is not None:
            logger.warning("write_confirmation_rejected", reason=problem)
            return _error_response("confirmation_invalid", problem)
        logger.info("write_confirmed")
        return None


def _json_response(payload: dict[str, Any]) -> TextContent:
    text = json.dumps(_sanitize(payload), indent=2, default=_json_default, allow_nan=False)
    return TextContent(type="text", text=text)


def _json_default(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _sanitize(value: Any) -> Any:
    """Recursively replace NaN/Infinity with None — not valid JSON, would break strict parsers."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _success_response(result: dict[str, Any]) -> TextContent:
    payload = _sanitize({"status": "success", **result})
    text = json.dumps(payload, indent=2, default=_json_default, allow_nan=False)
    return TextContent(type="text", text=text)


def _error_response(code: str, message: str) -> TextContent:
    payload = {"status": "error", "code": code, "message": message}
    return TextContent(type="text", text=json.dumps(payload, indent=2))
