"""Putting a set of magnet power supplies at their state and current, the way the consoles do.

A port of epik8s-opi Scripts/magapply.py (used by the consoles' pretune and restore
displays), so ARGUS and an operator at a console do the same thing:

1. STATE, for every supply whose state is not the wanted one:
   - ON -> OFF/STANDBY: CURRENT_SP is first set to 0, and the state is changed only once
     |CURRENT_RB| <= zero_tolerance (waited up to `timeout`). A supply still delivering current
     is never switched off.
   - STATE_SP is written, and STATE_RB is waited for (all supplies together, up to `timeout`).
2. CURRENT, only for supplies whose state was reached and that must be ON: CURRENT_SP is written
   (unless it already holds the value and force_current is off), and CURRENT_RB is waited for
   within `tolerance` (all together, up to `timeout`). The current of a supply that must be
   OFF/STANDBY means nothing and is not written.

STANDBY counts as OFF when checking a state. Supplies that do not complete a step in time are
reported, never retried behind the user's back.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from argus.providers.epics.exceptions import EpicsError
from argus.providers.epics.interface import EpicsProvider
from argus.services.magnet_sets import (
    COMMANDABLE_STATES,
    CURRENT_RB,
    CURRENT_SP,
    OFF_STATES,
    STATE_RB,
    STATE_SP,
    MagnetTarget,
)

DEFAULT_TOLERANCE = 0.1
DEFAULT_ZERO_TOLERANCE = 0.5
DEFAULT_TIMEOUT_S = 30.0
POLL_S = 0.5
CURRENT_EPSILON = 0.001


def state_reached(desired: str | None, actual: str | None) -> bool:
    if desired not in COMMANDABLE_STATES:
        return True
    if desired in OFF_STATES:
        return actual in OFF_STATES
    return actual == desired


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    zero_timeout: list[str] = field(default_factory=list)
    state_timeout: list[str] = field(default_factory=list)
    current_timeout: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    readback: dict[str, str] = field(default_factory=dict)
    written: list[str] = field(default_factory=list)

    @property
    def incomplete(self) -> bool:
        return bool(self.zero_timeout or self.state_timeout or self.current_timeout or self.errors)

    def as_dict(self, total: int) -> dict[str, Any]:
        return {
            "completed": len(self.applied),
            "of": total,
            "complete": not self.incomplete,
            "already_at_target": self.unchanged,
            "not_switched_off_still_delivering_current": self.zero_timeout,
            "state_not_reached": self.state_timeout,
            "current_not_within_tolerance": self.current_timeout,
            "errors": self.errors,
            "readbacks_of_incomplete": self.readback,
            "pv_writes": self.written,
        }

    def summary(self, total: int) -> str:
        text = f"Completed on {len(self.applied)} of {total} power supplies."
        if self.unchanged:
            text += f" {len(self.unchanged)} already at their target."
        if self.zero_timeout:
            text += f" NOT switched off, current not back to zero in time: {', '.join(self.zero_timeout[:10])}."
        if self.state_timeout:
            text += f" State not reached in time, current not set: {', '.join(self.state_timeout[:10])}."
        if self.current_timeout:
            text += f" Current not within tolerance in time: {', '.join(self.current_timeout[:10])}."
        if self.errors:
            text += f" Errors: {'; '.join(self.errors[:10])}."
        return text


async def apply_targets(
    epics: EpicsProvider,
    targets: list[MagnetTarget],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
    zero_tolerance: float = DEFAULT_ZERO_TOLERANCE,
    timeout: float = DEFAULT_TIMEOUT_S,
    retry: bool = False,
    force_current: bool = False,
    zero_off_setpoints: bool = False,
    poll: float = POLL_S,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ApplyResult:
    result = ApplyResult()
    failed: set[str] = set()

    async def read_state(t: MagnetTarget) -> str:
        return str((await epics.get(t.pv(STATE_RB))).value)

    async def read_current(t: MagnetTarget) -> float:
        return float((await epics.get(t.pv(CURRENT_RB))).value)

    async def write(t: MagnetTarget, field_name: str, value: Any) -> bool:
        try:
            await epics.put(t.pv(field_name), value)
        except EpicsError as exc:
            failed.add(t.base)
            result.errors.append(f"{t.base}: {exc}")
            return False
        result.written.append(f"{t.pv(field_name)} = {value}")
        return True

    async def wait(items: list[MagnetTarget], done: Callable[[MagnetTarget], Awaitable[bool]]) -> set[str]:
        """Polls all items together up to timeout; the bases not done."""
        deadline = clock() + timeout
        pending = list(items)
        while pending and clock() < deadline:
            await sleep(poll)
            still = []
            for item in pending:
                try:
                    if not await done(item):
                        still.append(item)
                except (EpicsError, ValueError, TypeError) as exc:
                    failed.add(item.base)
                    result.errors.append(f"{item.base}: {exc}")
            pending = still
        return {t.base for t in pending}

    todo: list[tuple[MagnetTarget, str]] = []
    for t in targets:
        try:
            actual = await read_state(t)
            if retry and state_reached(t.state, actual) and (
                t.current is None or t.state in OFF_STATES or abs(t.current - await read_current(t)) <= tolerance
            ):
                result.unchanged.append(t.base)
                continue
            todo.append((t, actual))
        except EpicsError as exc:
            result.errors.append(f"{t.base}: {exc}")

    # 1a. ON -> OFF/STANDBY: current to zero before the state changes. With zero_off_setpoints
    # ("all to 0 A and STANDBY"), also a supply already off whose setpoint is not 0.
    zeroing = []
    for t, actual in todo:
        if t.state not in OFF_STATES:
            continue
        if actual != "ON":
            if not zero_off_setpoints:
                continue
            try:
                if abs(float((await epics.get(t.pv(CURRENT_SP))).value)) <= CURRENT_EPSILON:
                    continue
            except (EpicsError, ValueError, TypeError) as exc:
                failed.add(t.base)
                result.errors.append(f"{t.base}: {exc}")
                continue
            await write(t, CURRENT_SP, 0.0)
            continue  # already off: nothing to wait for before its state
        if await write(t, CURRENT_SP, 0.0):
            zeroing.append(t)
    not_zero = await wait(zeroing, lambda t: _abs_le(read_current(t), zero_tolerance))

    # 1b. states.
    waiting = []
    for t, actual in todo:
        if t.base in failed or state_reached(t.state, actual):
            continue
        if t.base in not_zero:
            result.zero_timeout.append(t.base)
            continue
        if await write(t, STATE_SP, t.state):
            waiting.append(t)
    stuck = await wait(waiting, lambda t: _state_is(read_state(t), t.state))

    # 2. currents, only where the state was reached.
    ramping = []
    for t, _ in todo:
        if t.base in failed or t.base in not_zero:
            continue
        if t.base in stuck:
            result.state_timeout.append(t.base)
            continue
        if t.current is None or t.state in OFF_STATES:
            result.applied.append(t.base)
            continue
        try:
            live_sp = float((await epics.get(t.pv(CURRENT_SP))).value)
        except (EpicsError, ValueError, TypeError) as exc:
            failed.add(t.base)
            result.errors.append(f"{t.base}: {exc}")
            continue
        if (force_current or abs(t.current - live_sp) > CURRENT_EPSILON) and not await write(t, CURRENT_SP, t.current):
            continue
        ramping.append(t)
    slow = await wait(ramping, lambda t: _within(read_current(t), t.current, tolerance))
    for t in ramping:
        if t.base in failed:
            continue
        (result.current_timeout if t.base in slow else result.applied).append(t.base)

    for base in result.zero_timeout + result.state_timeout + result.current_timeout:
        t = next(x for x in targets if x.base == base)
        try:
            result.readback[base] = f"STATE_RB={await read_state(t)} CURRENT_RB={await read_current(t):.3f}"
        except (EpicsError, ValueError, TypeError) as exc:
            result.readback[base] = str(exc)
    return result


async def _abs_le(value: Awaitable[float], limit: float) -> bool:
    return abs(await value) <= limit


async def _state_is(value: Awaitable[str], desired: str | None) -> bool:
    return state_reached(desired, await value)


async def _within(value: Awaitable[float], desired: float | None, tolerance: float) -> bool:
    return desired is None or abs(desired - await value) <= tolerance
