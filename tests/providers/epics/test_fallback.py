"""pvAccess first, Channel Access behind it; a PV is never written over both."""

from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from argus.providers.base import ProviderHealth, ProviderStatus
from argus.providers.epics.exceptions import EpicsConnectionError, EpicsTimeoutError, EpicsUnconfiguredError, PVWriteRejectedError
from argus.providers.epics.fallback import FallbackEpicsProvider
from argus.providers.epics.models import PVInfo, PVValue, PVWriteResult


@dataclass
class _Fake:
    label: str
    known: set[str] = field(default_factory=set)
    configured: bool = True
    error: Exception | None = None
    put_error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)
    name: ClassVar[str] = "fake"

    def is_configured(self) -> bool:
        return self.configured

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.label, status=ProviderStatus.OK)

    def _check(self, pv: str) -> None:
        if pv not in self.known:
            raise self.error or EpicsTimeoutError(f"{self.label} timeout on {pv}")

    async def get(self, pv_name: str, timeout: float = 5.0) -> PVValue:
        self.calls.append(f"get {pv_name}")
        self._check(pv_name)
        return PVValue(name=pv_name, value=self.label)

    async def info(self, pv_name: str, timeout: float = 5.0) -> PVInfo:
        self.calls.append(f"info {pv_name}")
        self._check(pv_name)
        return PVInfo(name=pv_name, native_type=self.label)

    async def put(self, pv_name: str, value: Any, timeout: float = 5.0) -> PVWriteResult:
        self.calls.append(f"put {pv_name}")
        if self.put_error:
            raise self.put_error
        return PVWriteResult(name=pv_name, accepted=True, new_value=value)

    async def get_many(self, pv_names: list[str], timeout: float = 5.0) -> list[PVValue]:
        self.calls.append(f"get_many {pv_names}")
        self.timeouts.append(timeout)
        return [
            PVValue(name=n, value=self.label) if n in self.known else PVValue(name=n, value=None, status="disconnected")
            for n in pv_names
        ]


async def test_reads_use_pvaccess_first_and_channel_access_only_if_it_fails():
    pva, ca = _Fake("pva", {"BOTH"}), _Fake("ca", {"BOTH", "CA_ONLY"})
    epics = FallbackEpicsProvider(primary=pva, secondary=ca)

    assert (await epics.get("BOTH")).value == "pva" and ca.calls == []
    assert (await epics.get("CA_ONLY")).value == "ca"
    assert (await epics.info("CA_ONLY")).native_type == "ca"


async def test_an_unconfigured_protocol_is_skipped():
    ca = _Fake("ca", {"A"})
    epics = FallbackEpicsProvider(primary=_Fake("pva", configured=False), secondary=ca)
    assert (await epics.get("A")).value == "ca"
    assert (await epics.put("A", 1)).accepted

    with pytest.raises(EpicsUnconfiguredError):
        await FallbackEpicsProvider(_Fake("pva", configured=False), _Fake("ca", configured=False)).get("A")


async def test_when_both_fail_the_error_names_both():
    epics = FallbackEpicsProvider(_Fake("pva"), _Fake("ca"))
    with pytest.raises(EpicsTimeoutError, match="pva timeout on X; ca timeout on X"):
        await epics.get("X")

    mixed = FallbackEpicsProvider(_Fake("pva"), _Fake("ca", error=EpicsConnectionError("ca refused")))
    with pytest.raises(EpicsConnectionError, match="pva timeout on X; ca refused"):
        await mixed.get("X")


async def test_a_pv_that_answers_on_pvaccess_is_written_there_only_even_if_that_fails():
    pva, ca = _Fake("pva", {"A"}, put_error=EpicsTimeoutError("may have been written")), _Fake("ca", {"A"})
    with pytest.raises(EpicsTimeoutError, match="may have been written"):
        await FallbackEpicsProvider(pva, ca).put("A", 1)
    assert pva.calls == ["get A", "put A"] and ca.calls == []

    refused = _Fake("pva", {"A"}, put_error=PVWriteRejectedError("read-only"))
    with pytest.raises(PVWriteRejectedError):
        await FallbackEpicsProvider(refused, ca).put("A", 1)
    assert ca.calls == []


async def test_a_pv_that_does_not_answer_on_pvaccess_is_written_over_channel_access():
    pva, ca = _Fake("pva"), _Fake("ca", {"A"})
    assert (await FallbackEpicsProvider(pva, ca).put("A", 1)).accepted
    assert pva.calls == ["get A"] and ca.calls == ["put A"]


async def test_batches_ask_channel_access_only_for_what_pvaccess_missed_within_the_same_time():
    pva, ca = _Fake("pva", {"A"}), _Fake("ca", {"B"})
    values = await FallbackEpicsProvider(pva, ca).get_many(["A", "B", "C"], timeout=3.0)
    assert [(v.name, v.value, v.status) for v in values] == [("A", "pva", None), ("B", "ca", None), ("C", None, "disconnected")]
    assert ca.calls == ["get_many ['B', 'C']"]
    assert pva.timeouts == [1.5] and ca.timeouts == [1.5]
