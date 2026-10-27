from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LogbookEntry:
    id: str
    title: str
    text: str
    logbooks: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    author: str | None = None
    # A link to the entry's page, when the source has one (the Knowledge
    # Hub's ready-made Markdown link); None from Olog.
    url: str | None = None
