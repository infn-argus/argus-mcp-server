"""The ARGUS Knowledge Hub workspaces this server asks in.

Registered only when the hub is configured (KNOWLEDGE_HUB_URL and
KNOWLEDGE_HUB_TOKEN): list_beamline_devices, search_documentation and the
logbook reads fan out across these workspaces, and list_beamline_devices
takes one of their ids to narrow to it.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ProviderUnconfiguredError
from argus.mcp_server.tools.registry import ToolDefinition


async def _list_knowledge_hub_workspaces(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    hub = ctx.knowledge_hub
    if hub is None or not hub.is_configured():
        raise ProviderUnconfiguredError(
            "The ARGUS Knowledge Hub is not configured (set KNOWLEDGE_HUB_URL and KNOWLEDGE_HUB_TOKEN)."
        )
    workspaces = await hub.list_workspaces()
    return {
        "workspaces": [dataclasses.asdict(w) for w in workspaces],
        "restricted_to": hub.configured_workspaces() or None,
    }


TOOLS = [
    ToolDefinition(
        name="list_knowledge_hub_workspaces",
        description=(
            "The ARGUS Knowledge Hub workspaces this server reads devices, documentation and logbook "
            "entries from (id, name, whether it is a shared catalogue). Searches already cover all of "
            "them; call this only to say where an answer came from or to narrow list_beamline_devices "
            "to one workspace."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        handler=_list_knowledge_hub_workspaces,
    ),
]
