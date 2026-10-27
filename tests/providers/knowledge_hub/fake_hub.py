"""An in-process stand-in for the ARGUS Knowledge Hub's /mcp endpoint.

Speaks the same JSON-RPC subset the real hub does (argus-knowledge-hub's
routers/mcp.py): initialize, notifications/*, ping, tools/call with a
`workspace` argument, and list_workspaces. Tools are plain functions
(arguments, workspace) -> result; raising ToolFailure answers isError, the
way a hub tool failing does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from argus.config.settings import KnowledgeHubSettings
from argus.providers.knowledge_hub.client import KnowledgeHubClient

URL = "http://hub.test/mcp"

Tool = Callable[[dict[str, Any], str], Any]


class ToolFailure(Exception):
    pass


class FakeHub:
    def __init__(
        self,
        tools: dict[str, Tool] | None = None,
        workspaces: list[str] | None = None,
        *,
        session_id: str | None = None,
        sse: bool = False,
    ) -> None:
        self.tools = tools or {}
        self.workspaces = workspaces if workspaces is not None else ["btf"]
        self.session_id = session_id
        self.sse = sse
        self.requests: list[dict[str, Any]] = []

    def tool_calls(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r.get("tool") == name]

    def _answer(self, request_id: Any, result: dict, headers: dict[str, str] | None = None) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": request_id, "result": result}
        if self.sse:
            text = f"event: message\ndata: {json.dumps(message)}\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream", **(headers or {})})
        return httpx.Response(200, json=message, headers=headers or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        request_id = body.get("id")
        record: dict[str, Any] = {"method": method, "headers": dict(request.headers)}
        self.requests.append(record)

        if method == "initialize":
            headers = {"mcp-session-id": self.session_id} if self.session_id else {}
            return self._answer(request_id, {"protocolVersion": "2025-06-18", "capabilities": {}}, headers)
        if method and method.startswith("notifications/"):
            return httpx.Response(202)
        if method == "ping":
            return self._answer(request_id, {})
        if method != "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601}})

        params = body.get("params") or {}
        name = params.get("name")
        arguments = dict(params.get("arguments") or {})
        workspace = arguments.pop("workspace", None) or request.headers.get("x-workspace-id") or self.workspaces[0]
        record.update(tool=name, arguments=arguments, workspace=workspace)

        if name == "list_workspaces":
            payload: Any = {
                "current": workspace,
                "workspaces": [{"id": w, "name": w.upper(), "shared": w.endswith("catalogue")} for w in self.workspaces],
            }
        else:
            if workspace not in self.workspaces:
                return self._error_result(request_id, f"You may not ask in workspace {workspace!r}")
            try:
                payload = self.tools[name](arguments, workspace)
            except ToolFailure as exc:
                return self._error_result(request_id, str(exc))
        return self._answer(request_id, {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False})

    def _error_result(self, request_id: Any, text: str) -> httpx.Response:
        return self._answer(request_id, {"content": [{"type": "text", "text": text}], "isError": True})


def hub_client(workspaces: str | None = None, **overrides: Any) -> KnowledgeHubClient:
    settings = KnowledgeHubSettings(
        _env_file=None,
        KNOWLEDGE_HUB_URL=URL,
        KNOWLEDGE_HUB_TOKEN="argus_pat_test",
        **({"KNOWLEDGE_HUB_WORKSPACES": workspaces} if workspaces is not None else {}),
        **overrides,
    )
    return KnowledgeHubClient(settings)
