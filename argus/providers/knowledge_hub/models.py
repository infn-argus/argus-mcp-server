from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HubWorkspace:
    id: str
    name: str
    shared: bool = False
