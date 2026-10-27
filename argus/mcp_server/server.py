"""Single ARGUS MCP server entrypoint.

Replaces the old duplicated server_by_stdio.py / server_by_sse.py: one
ToolRegistry backs both transports, selected via Settings.transport
(ARGUS_TRANSPORT env var or --transport CLI flag).
"""

from __future__ import annotations

from typing import Sequence

from mcp.server import Server
from mcp.types import TextContent, Tool

from argus.__about__ import __version__
from argus.config.logging import configure_logging
from argus.config.settings import Settings
from argus.core.context import AppContext
from argus.core.request_context import caller_token_scope, confirm_writes_scope
from argus.mcp_server.correlation import (
    current_http_request,
    extract_caller_token,
    extract_confirm_writes,
    extract_correlation_headers,
)
from argus.mcp_server.tools import beamline_tools
from argus.mcp_server.tools.device_tools import TOOLS as DEVICE_TOOLS
from argus.mcp_server.tools.docs_tools import TOOLS as DOCS_TOOLS
from argus.mcp_server.tools.history_tools import TOOLS as HISTORY_TOOLS
from argus.mcp_server.tools.hub_tools import TOOLS as HUB_TOOLS
from argus.mcp_server.tools.knowledge_tools import TOOLS as KNOWLEDGE_TOOLS
from argus.mcp_server.tools.logs_tools import TOOLS as LOGS_TOOLS
from argus.mcp_server.tools.ops_tools import TOOLS as OPS_TOOLS
from argus.mcp_server.tools.pv_tools import TOOLS as PV_TOOLS
from argus.mcp_server.tools.registry import ToolRegistry
from argus.mcp_server.tools.saverestore_tools import TOOLS as SAVERESTORE_TOOLS
from argus.mcp_server.transports.sse import run_sse
from argus.mcp_server.transports.stdio import run_stdio

SERVER_NAME = "argus"


def build_registry(settings: Settings | None = None) -> ToolRegistry:
    """The tool set for these settings; without settings, the legacy (YAML inventory) one."""
    inventory_source = settings.inventory_backend if settings else "yaml"
    registry = ToolRegistry()
    # beamline_tools first: list_beamline_devices is the intended first call
    # for most device/IOC/zone questions, and tool list position can bias
    # model tool-selection priority, not just the description text.
    registry.register_many(beamline_tools.tools(inventory_source))
    registry.register_many(PV_TOOLS)
    registry.register_many(DEVICE_TOOLS)
    registry.register_many(HISTORY_TOOLS)
    registry.register_many(OPS_TOOLS)
    registry.register_many(LOGS_TOOLS)
    registry.register_many(DOCS_TOOLS)
    registry.register_many(KNOWLEDGE_TOOLS)
    registry.register_many(SAVERESTORE_TOOLS)
    if settings and settings.knowledge_hub.url and settings.knowledge_hub.token:
        registry.register_many(HUB_TOOLS)
    return registry


def build_mcp_server(registry: ToolRegistry, ctx: AppContext) -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return registry.list_tools(confirm_writes=extract_confirm_writes(current_http_request(server)))

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> Sequence[TextContent]:
        request = current_http_request(server)
        correlation = extract_correlation_headers(request)
        with caller_token_scope(extract_caller_token(request)), confirm_writes_scope(extract_confirm_writes(request)):
            return await registry.dispatch(name, arguments or {}, ctx, correlation=correlation)

    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="argus")
    parser.add_argument("--transport", choices=["stdio", "sse"], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    settings = Settings()
    if args.transport:
        settings.transport = args.transport
    if args.host:
        settings.sse_host = args.host
    if args.port:
        settings.sse_port = args.port

    configure_logging(settings)

    ctx = AppContext.build(settings)
    registry = build_registry(settings)
    server = build_mcp_server(registry, ctx)

    if settings.transport == "stdio":
        import asyncio

        asyncio.run(run_stdio(server, SERVER_NAME, __version__))
    else:
        run_sse(server, settings.sse_host, settings.sse_port)


if __name__ == "__main__":
    main()
