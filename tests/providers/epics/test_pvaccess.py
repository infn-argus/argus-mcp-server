import asyncio

import pytest

from argus.config.settings import EpicsSettings
from argus.providers.epics.exceptions import EpicsConnectionError, EpicsTimeoutError, EpicsUnconfiguredError
from argus.providers.epics.pvaccess import PvAccessProvider


class _FakeValue:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, key):
        raise KeyError(key)


class _FakeContext:
    """Mirrors p4p's real Context.get/put signatures exactly (name, request=None,
    and put's process/wait/get) -- no timeout/throw kwargs. A provider call
    that (re)introduces those unsupported kwargs raises TypeError here just
    like it does against the real p4p Context, which is the regression this
    file guards against.
    """

    def __init__(self) -> None:
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, object]] = []
        self.responses: dict[str, object] = {}
        self.delay = 0.0
        self.error: Exception | None = None
        self.disconnected: list[str] = []

    def disconnect(self, name=None):
        self.disconnected.append(name)

    async def get(self, name, request=None):
        self.get_calls.append(name)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.responses.get(name, _FakeValue(0.0))

    async def put(self, name, values, request=None, process=None, wait=None, get=True):
        self.put_calls.append((name, values))


def _configured_provider() -> tuple[PvAccessProvider, _FakeContext]:
    provider = PvAccessProvider(EpicsSettings(_env_file=None, EPICS_PVA_ADDR_LIST="127.0.0.1"))
    fake_ctx = _FakeContext()
    provider._context = fake_ctx
    return provider, fake_ctx


async def test_unconfigured_provider_raises_on_get():
    provider = PvAccessProvider(EpicsSettings(_env_file=None))
    assert provider.is_configured() is False
    with pytest.raises(EpicsUnconfiguredError):
        await provider.get("some:pv")


async def test_get_returns_pv_value():
    provider, ctx = _configured_provider()
    ctx.responses["QUATB002:CURRENT"] = _FakeValue(4.2)

    result = await provider.get("QUATB002:CURRENT")

    assert result.name == "QUATB002:CURRENT"
    assert result.value == 4.2
    assert ctx.get_calls == ["QUATB002:CURRENT"]


async def test_get_timeout_raises_epics_timeout_error():
    provider, ctx = _configured_provider()
    ctx.delay = 10.0

    with pytest.raises(EpicsTimeoutError):
        await provider.get("QUATB002:CURRENT", timeout=0.01)


async def test_get_translates_exception_to_connection_error():
    provider, ctx = _configured_provider()
    ctx.error = RuntimeError("disconnected")

    with pytest.raises(EpicsConnectionError):
        await provider.get("QUATB002:CURRENT")


async def test_put_writes_value():
    provider, ctx = _configured_provider()

    result = await provider.put("QUATB002:CURRENT", 5.0)

    assert result.accepted is True
    assert ctx.put_calls == [("QUATB002:CURRENT", 5.0)]


async def test_put_timeout_raises_epics_timeout_error():
    provider, ctx = _configured_provider()

    async def slow_put(name, values, request=None, process=None, wait=None, get=True):
        await asyncio.sleep(10.0)

    ctx.put = slow_put

    with pytest.raises(EpicsTimeoutError):
        await provider.put("QUATB002:CURRENT", 5.0, timeout=0.01)


async def test_get_many_marks_disconnected_pvs_on_individual_failure():
    provider, ctx = _configured_provider()
    ctx.responses["OK"] = _FakeValue(1.0)
    real_get = ctx.get

    async def flaky_get(name, request=None):
        if name == "BAD":
            raise RuntimeError("no such pv")
        return await real_get(name, request=request)

    ctx.get = flaky_get

    results = await provider.get_many(["OK", "BAD"])

    assert results[0].status is None
    assert results[0].value == 1.0
    assert results[1].status == "disconnected"


async def test_get_many_marks_disconnected_on_per_pv_timeout():
    provider, ctx = _configured_provider()
    ctx.responses["OK"] = _FakeValue(1.0)
    real_get = ctx.get

    async def one_slow_get(name, request=None):
        if name == "SLOW":
            await asyncio.sleep(10.0)
        return await real_get(name, request=request)

    ctx.get = one_slow_get

    results = await provider.get_many(["OK", "SLOW"], timeout=0.01)

    assert results[0].value == 1.0
    assert results[1].status == "disconnected"


async def test_a_timeout_on_a_new_channel_drops_it_without_retrying():
    provider, ctx = _configured_provider()
    ctx.delay = 0.05
    with pytest.raises(EpicsTimeoutError):
        await provider.get("NEW:PV", timeout=0.01)
    assert ctx.get_calls == ["NEW:PV"] and ctx.disconnected == ["NEW:PV"]


async def test_a_timeout_on_a_cached_channel_retries_once_on_a_fresh_one():
    provider, ctx = _configured_provider()
    await provider.get("OLD:PV")

    real_get = ctx.get
    calls = {"n": 0}

    async def stale_then_fine(name, request=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(1)
        return await real_get(name, request)

    ctx.get = stale_then_fine
    result = await provider.get("OLD:PV", timeout=0.02)
    assert result.value == 0.0 and calls["n"] == 2 and ctx.disconnected == ["OLD:PV"]


async def test_a_put_timeout_drops_the_channel_and_is_never_retried():
    provider, ctx = _configured_provider()

    async def hang(name, values, **_):
        ctx.put_calls.append((name, values))
        await asyncio.sleep(1)

    ctx.put = hang
    with pytest.raises(EpicsTimeoutError):
        await provider.put("A:PV", 1.0, timeout=0.01)
    assert ctx.put_calls == [("A:PV", 1.0)] and ctx.disconnected == ["A:PV"]


class _Raw(dict):
    def __getitem__(self, path):
        node = dict(self)
        for part in path.split("."):
            node = node[part]
        return node

    def todict(self):
        return dict(self)


def _nt(cls, value, raw, **extra):
    wrapped = cls(value)
    wrapped.raw = _Raw(raw)
    wrapped.severity, wrapped.status, wrapped.timestamp = 1, 7, 1793915739.5
    for key, val in extra.items():
        setattr(wrapped, key, val)
    return wrapped


class _NtFloat(float):
    pass


class _NtEnum(int):
    pass


async def test_values_are_plain_with_timestamp_and_severity_and_an_enum_gives_its_label():
    provider, ctx = _configured_provider()
    ctx.responses["F"] = _nt(_NtFloat, -50.85, {})
    ctx.responses["E"] = _nt(_NtEnum, 6, {}, choice="SP_NOT_REACHED")

    number = await provider.get("F")
    assert type(number.value) is float and number.value == -50.85
    assert number.timestamp == 1793915739.5 and number.severity == "1" and number.status == "7"
    assert (await provider.get("E")).value == "SP_NOT_REACHED"


async def test_info_returns_what_pvaccess_knows_and_unset_limits_are_absent():
    provider, ctx = _configured_provider()
    nan = float("nan")
    ctx.responses["F"] = _nt(_NtFloat, -50.85, {
        "alarm": {"severity": 0, "status": 0, "message": ""},
        "display": {"limitLow": 0.0, "limitHigh": 0.0, "description": "Current Readback", "units": "A", "precision": 3},
        "control": {"limitLow": -100.0, "limitHigh": 100.0},
        "valueAlarm": {"lowAlarmLimit": nan, "highAlarmLimit": nan, "lowWarningLimit": -90.0, "highWarningLimit": 90.0},
    })
    info = await provider.info("F")
    assert info.native_type == "float" and info.description == "Current Readback"
    assert (info.units, info.precision) == ("A", 3)
    assert info.display_limits is None and info.alarm_limits is None
    assert info.control_limits == (-100.0, 100.0) and info.warning_limits == (-90.0, 90.0)
    assert info.alarm_message is None and info.details["display"]["units"] == "A"
    # NaN isn't valid JSON, and these go straight to the client.
    assert info.details["valueAlarm"]["highAlarmLimit"] is None

    ctx.responses["E"] = _nt(_NtEnum, 6, {
        "value": {"index": 6, "choices": ["OFF", "ON"]},
        "alarm": {"message": "STATE"},
        "display": {"description": "State readback"},
    }, choice="ON")
    info = await provider.info("E")
    assert info.native_type == "enum" and info.choices == ["OFF", "ON"] and info.alarm_message == "STATE"
