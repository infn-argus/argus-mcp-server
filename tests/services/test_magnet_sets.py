"""Magnet sets: reading the consoles' files, and putting supplies at a set the way the consoles do."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from argus.core.errors import ValidationError
from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.epics.exceptions import EpicsConnectionError
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult
from argus.services.magnet_apply import apply_targets
from argus.services.magnet_sets import MagnetTarget, parse_magnet_file

CSV = """Name,Prefix,Current,State
CVVTM001,BTF:MAG:EASY,-0.029348,ON
CHHTM001,BTF:MAG:EASY,0.000000,STANDBY
QUATM002,BTF:MAG:DANFYSIK,-0.706522,ON
DHPTT001,DAFNE:ACC:MAG:OCEME642,6.255435,INTERLOCK
"""


# --- files -------------------------------------------------------------------

def test_the_consoles_csv_gives_each_supply_its_prefix_current_and_state():
    parsed = parse_magnet_file(CSV)
    assert parsed.format == "csv"
    first = parsed.targets[0]
    assert (first.base, first.current, first.state) == ("BTF:MAG:EASY:CVVTM001", -0.029348, "ON")
    assert first.pv("CURRENT_SP") == "BTF:MAG:EASY:CVVTM001:CURRENT_SP"
    assert parsed.targets[1].state == "STANDBY"
    # A state that cannot be commanded keeps its current and leaves the state alone.
    assert (parsed.targets[3].current, parsed.targets[3].state) == (6.255435, None)
    assert any("INTERLOCK" in p for p in parsed.problems)


def test_the_legacy_dat_takes_its_prefix_from_the_inventory_and_its_sign_from_pol():
    text = "CVVTM001\t-2.700\t*\t2\n1 QUATM003 0.5 - 2\nCHHTM001 0.000 * 1\n# comment\nNOPE01 1.0 * 2\n"
    prefixes = {"cvvtm001": "BTF:MAG:EASY", "quatm003": "BTF:MAG:DANFYSIK", "chhtm001": "BTF:MAG:EASY"}
    parsed = parse_magnet_file(text, prefix_of=lambda n: prefixes.get(n.lower()))
    by_name = {t.name: t for t in parsed.targets}
    assert parsed.format == "dat"
    assert (by_name["CVVTM001"].current, by_name["CVVTM001"].state) == (-2.7, "ON")
    assert by_name["QUATM003"].current == -0.5  # pol '-' inverts
    assert by_name["CHHTM001"].state == "OFF"  # code 1
    assert "NOPE01" not in by_name and any("NOPE01" in p for p in parsed.problems)


def test_a_supply_listed_twice_is_refused():
    with pytest.raises(ValidationError, match="more than once"):
        parse_magnet_file(CSV + "CVVTM001,BTF:MAG:EASY,1.0,ON\n")


# --- the apply sequence --------------------------------------------------------

@dataclass
class Supplies:
    """Power supplies whose readbacks follow their setpoints, unless told to stick."""

    name: ClassVar[str] = "sim"
    state: dict[str, str] = field(default_factory=dict)
    sp: dict[str, float] = field(default_factory=dict)
    rb: dict[str, float] = field(default_factory=dict)
    stuck_current: set[str] = field(default_factory=set)
    stuck_state: set[str] = field(default_factory=set)
    writes: list[tuple[str, Any]] = field(default_factory=list)
    limits: dict[str, tuple[float, float]] = field(default_factory=dict)

    def add(self, base: str, state: str, current: float) -> None:
        self.state[base], self.sp[base], self.rb[base] = state, current, current if state == "ON" else 0.0

    def is_configured(self) -> bool:
        return True

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, status=ProviderStatus.OK)

    def _split(self, pv: str) -> tuple[str, str]:
        base, _, f = pv.rpartition(":")
        if base not in self.state:
            raise EpicsConnectionError(f"PV '{pv}' not found")
        return base, f

    def _settle(self, base: str) -> None:
        if base not in self.stuck_current:
            self.rb[base] = self.sp[base] if self.state[base] == "ON" else 0.0

    async def get(self, pv_name: str, timeout: float = 5.0) -> PVValue:
        base, f = self._split(pv_name)
        value = {"STATE_RB": self.state[base], "STATE_SP": self.state[base], "CURRENT_SP": self.sp[base],
                 "CURRENT_RB": self.rb[base]}[f]
        return PVValue(name=pv_name, value=value)

    async def put(self, pv_name: str, value: Any, timeout: float = 5.0) -> PVWriteResult:
        base, f = self._split(pv_name)
        self.writes.append((pv_name, value))
        if f == "CURRENT_SP":
            self.sp[base] = float(value)
        elif f == "STATE_SP" and base not in self.stuck_state:
            self.state[base] = str(value)
        self._settle(base)
        return PVWriteResult(name=pv_name, accepted=True, new_value=value)

    async def info(self, pv_name: str, timeout: float = 5.0) -> PVInfo:
        base, _ = self._split(pv_name)
        return PVInfo(name=pv_name, control_limits=self.limits.get(base), choices=["OFF", "STANDBY", "ON"])

    async def get_many(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]:
        out = []
        for pv in pv_names:
            try:
                out.append(await self.get(pv))
            except EpicsConnectionError:
                out.append(PVValue(name=pv, value=None, status="disconnected"))
        return out


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def _apply(supplies: Supplies, targets: list[MagnetTarget], **kw):
    clock = Clock()
    return await apply_targets(supplies, targets, timeout=5.0, sleep=clock.sleep, clock=clock, **kw)


def _t(base: str, current: float | None, state: str | None) -> MagnetTarget:
    prefix, _, name = base.rpartition(":")
    return MagnetTarget(name, prefix, current, state)


async def test_an_on_supply_goes_to_zero_before_standby_and_an_off_one_is_switched_on_before_its_current():
    s = Supplies()
    s.add("P:A", "ON", 4.0)
    s.add("P:B", "STANDBY", 0.0)
    result = await _apply(s, [_t("P:A", 0.0, "STANDBY"), _t("P:B", 2.5, "ON")])
    assert not result.incomplete and sorted(result.applied) == ["P:A", "P:B"]
    assert s.writes.index(("P:A:CURRENT_SP", 0.0)) < s.writes.index(("P:A:STATE_SP", "STANDBY"))
    assert s.writes.index(("P:B:STATE_SP", "ON")) < s.writes.index(("P:B:CURRENT_SP", 2.5))
    assert (s.state["P:A"], s.state["P:B"], s.rb["P:B"]) == ("STANDBY", "ON", 2.5)
    # The current of a supply that must be off is never written beyond the zeroing.
    assert [w for w in s.writes if w[0] == "P:A:CURRENT_SP"] == [("P:A:CURRENT_SP", 0.0)]


async def test_a_supply_still_delivering_current_is_never_switched_off():
    s = Supplies()
    s.add("P:A", "ON", 4.0)
    s.stuck_current.add("P:A")
    result = await _apply(s, [_t("P:A", 0.0, "STANDBY")])
    assert result.zero_timeout == ["P:A"] and result.incomplete
    assert ("P:A:STATE_SP", "STANDBY") not in s.writes
    assert "CURRENT_RB=4.000" in result.readback["P:A"]


async def test_a_state_not_reached_gets_no_current():
    s = Supplies()
    s.add("P:B", "STANDBY", 0.0)
    s.stuck_state.add("P:B")
    result = await _apply(s, [_t("P:B", 2.0, "ON")])
    assert result.state_timeout == ["P:B"]
    assert ("P:B:CURRENT_SP", 2.0) not in s.writes


async def test_a_current_already_there_is_not_rewritten_unless_forced():
    s = Supplies()
    s.add("P:A", "ON", 1.0)
    assert (await _apply(s, [_t("P:A", 1.0, "ON")])).applied == ["P:A"] and s.writes == []
    await _apply(s, [_t("P:A", 1.0, "ON")], force_current=True)
    assert s.writes == [("P:A:CURRENT_SP", 1.0)]


async def test_all_to_zero_and_standby_also_zeroes_a_supply_already_off():
    s = Supplies()
    s.add("P:A", "STANDBY", 0.0)
    s.sp["P:A"] = 3.0
    result = await _apply(s, [_t("P:A", 0.0, "STANDBY")], zero_off_setpoints=True)
    assert not result.incomplete and s.sp["P:A"] == 0.0 and ("P:A:STATE_SP", "STANDBY") not in s.writes


async def test_a_supply_that_does_not_answer_is_an_error_and_the_others_go_on():
    s = Supplies()
    s.add("P:A", "ON", 1.0)
    result = await _apply(s, [_t("P:GONE", 1.0, "ON"), _t("P:A", 2.0, "ON")])
    assert result.applied == ["P:A"] and any("P:GONE" in e for e in result.errors)
