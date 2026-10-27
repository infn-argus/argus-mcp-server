from __future__ import annotations

import dataclasses
from typing import Any

from argus.core.context import AppContext
from argus.core.errors import ValidationError
from argus.mcp_server.tools.registry import ToolDefinition

_SEARCH_DOCS_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Free-text search query."},
        "device": {"type": "string", "description": "Optional device name to bias the search toward."},
        "top_k": {"type": "integer", "description": "Maximum number of results to return."},
    },
    "required": ["query"],
}


async def _search_documentation(arguments: dict[str, Any], ctx: AppContext) -> dict[str, Any]:
    query = arguments.get("query")
    if not query or not isinstance(query, str):
        raise ValidationError("query cannot be empty and must be a string.")

    results = await ctx.documentation_service.search_documentation(
        query, device=arguments.get("device"), top_k=arguments.get("top_k", 5)
    )
    return {"results": [dataclasses.asdict(r) for r in results]}


TOOLS = [
    ToolDefinition(
        name="search_documentation",
        description=(
            "Search technical documentation and manuals. When a result's source_path is a Markdown link, "
            "cite it by writing that link exactly as given."
        ),
        input_schema=_SEARCH_DOCS_SCHEMA,
        handler=_search_documentation,
    ),
]
