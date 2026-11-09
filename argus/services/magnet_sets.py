"""Magnet settings files: what a set of power supplies should be at.

Two formats, the ones the control-room consoles load (epik8s-opi
unimag-opi/pretune/scripts/LoadPretuneDataSet.py):

* CSV with a header ``Name,Prefix,Current,State`` (one power supply a row; the
  PVs are ``<Prefix>:<Name>:CURRENT_SP`` and so on), e.g.
  ``CVVTM001,BTF:MAG:EASY,-0.029348,ON``;
* the legacy whitespace-separated ``.dat``: ``[index] Name Current Pol StateCode``
  with no prefix (it comes from the device inventory). Pol ``-`` inverts the
  current's sign, ``*`` or ``+`` takes it as written; StateCode 1 is OFF, 2 is ON.

A state other than ON/STANDBY/OFF (INTERLOCK, FAULT...) cannot be commanded:
the row keeps its current and its state is left alone.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass, field

from argus.core.errors import ValidationError

COMMANDABLE_STATES = ("ON", "STANDBY", "OFF")
OFF_STATES = ("OFF", "STANDBY")

CURRENT_SP = "CURRENT_SP"
CURRENT_RB = "CURRENT_RB"
STATE_SP = "STATE_SP"
STATE_RB = "STATE_RB"
FIELDS = (CURRENT_SP, CURRENT_RB, STATE_SP, STATE_RB)


@dataclass
class MagnetTarget:
    """One power supply and what it should be at. current None: leave the current alone;
    state None: leave the state alone."""

    name: str
    prefix: str
    current: float | None
    state: str | None

    @property
    def base(self) -> str:
        return f"{self.prefix}:{self.name}"

    def pv(self, field_name: str) -> str:
        return f"{self.base}:{field_name}"


@dataclass
class MagnetFile:
    targets: list[MagnetTarget] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    format: str = "csv"


def split_base(pv_name: str) -> tuple[str, str] | None:
    """``DAFNE:TL:MAG:COR:CHHTL001:CURRENT_SP`` -> (``DAFNE:TL:MAG:COR:CHHTL001``, ``CURRENT_SP``)."""
    base, _, suffix = pv_name.rpartition(":")
    return (base, suffix) if base and suffix in FIELDS else None


def target_from_base(base: str, current: float | None, state: str | None) -> MagnetTarget:
    prefix, _, name = base.rpartition(":")
    return MagnetTarget(name=name, prefix=prefix, current=current, state=state)


def parse_magnet_file(text: str, prefix_of: Callable[[str], str | None] | None = None) -> MagnetFile:
    """Parse either format. prefix_of(name) gives a power supply's prefix for a .dat file."""
    if not text or not text.strip():
        raise ValidationError("The magnet file is empty.")
    first = next(line for line in text.splitlines() if line.strip())
    if "," in first and "name" in first.lower() and "current" in first.lower():
        result = _parse_csv(text)
    else:
        result = _parse_dat(text, prefix_of)
    seen: dict[str, int] = {}
    for target in result.targets:
        seen[target.base] = seen.get(target.base, 0) + 1
    duplicates = sorted(b for b, n in seen.items() if n > 1)
    if duplicates:
        raise ValidationError(f"The file lists these power supplies more than once: {', '.join(duplicates)}.")
    if not result.targets:
        raise ValidationError("No power supply could be read from the file. " + " ".join(result.problems[:5]))
    return result


def _state(value: str, where: str, problems: list[str]) -> str | None:
    state = (value or "").strip().upper()
    if state in COMMANDABLE_STATES:
        return state
    problems.append(f"{where}: state {value!r} cannot be commanded, so it is left as it is.")
    return None


def _parse_csv(text: str) -> MagnetFile:
    result = MagnetFile(format="csv")
    reader = csv.DictReader(io.StringIO(text.strip()))
    columns = {c.strip().lower(): c for c in reader.fieldnames or []}
    missing = [c for c in ("name", "prefix", "current") if c not in columns]
    if missing:
        raise ValidationError(f"The CSV has no column {', '.join(missing)} (expected Name,Prefix,Current,State).")
    for number, row in enumerate(reader, start=2):
        name = (row.get(columns["name"]) or "").strip()
        prefix = (row.get(columns["prefix"]) or "").strip().strip(":")
        if not name and not prefix:
            continue
        where = f"line {number} ({name or '?'})"
        if not name or not prefix:
            result.problems.append(f"{where}: no name or prefix, skipped.")
            continue
        try:
            current = float((row.get(columns["current"]) or "").strip())
        except ValueError:
            result.problems.append(f"{where}: current {row.get(columns['current'])!r} is not a number, skipped.")
            continue
        state = _state(row.get(columns.get("state", ""), "") if "state" in columns else "", where, result.problems)
        result.targets.append(MagnetTarget(name=name, prefix=prefix, current=current, state=state))
    return result


def _parse_dat(text: str, prefix_of: Callable[[str], str | None] | None) -> MagnetFile:
    result = MagnetFile(format="dat")
    for number, line in enumerate(text.splitlines(), start=1):
        if "#" in line or not line.strip():
            continue
        parts = line.split()
        if len(parts) == 5:
            parts = parts[1:]
        if len(parts) != 4:
            result.problems.append(f"line {number}: not 'Name Current Pol StateCode', skipped.")
            continue
        name, value, polarity, code = parts
        where = f"line {number} ({name})"
        try:
            current = float(value)
        except ValueError:
            result.problems.append(f"{where}: current {value!r} is not a number, skipped.")
            continue
        if polarity == "-":
            current = -current
        state = {"1": "OFF", "2": "ON"}.get(code)
        if state is None:
            result.problems.append(f"{where}: state code {code!r} is not 1 (OFF) or 2 (ON), so it is left as it is.")
        prefix = prefix_of(name) if prefix_of else None
        if not prefix:
            result.problems.append(f"{where}: not in this beamline's device inventory, so its PV prefix is unknown; skipped.")
            continue
        result.targets.append(MagnetTarget(name=name, prefix=prefix.strip(":"), current=current, state=state))
    return result
