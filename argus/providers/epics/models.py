from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class PVValue:
    name: str
    value: Any
    timestamp: datetime | None = None
    severity: str | None = None
    status: str | None = None
    units: str | None = None


@dataclass
class PVInfo:
    name: str
    native_type: str | None = None
    element_count: int | None = None
    host: str | None = None
    access: str | None = None
    units: str | None = None
    precision: int | None = None
    display_limits: tuple[float, float] | None = None
    alarm_limits: tuple[float, float] | None = None
    # pvAccess only: Channel Access has no equivalent, or host/access instead.
    description: str | None = None
    choices: list[str] | None = None  # an enum's states
    alarm_message: str | None = None
    control_limits: tuple[float, float] | None = None
    warning_limits: tuple[float, float] | None = None
    details: dict[str, Any] | None = None  # the PV's whole pvAccess structure


@dataclass
class PVWriteResult:
    name: str
    accepted: bool
    new_value: Any = None
