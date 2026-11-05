import aioca
import pytest

from argus.config.settings import EpicsSettings
from epicscorelibs.ca import cadef

from argus.providers.epics import channel_access
from argus.providers.epics.channel_access import ChannelAccessProvider
from argus.providers.epics.exceptions import EpicsConnectionError, EpicsTimeoutError, EpicsUnconfiguredError, PVWriteRejectedError


class _FakeAugmentedFloat(float):
    pass


def _make_value(value, **attrs):
    v = _FakeAugmentedFloat(value)
    defaults = {"ok": True, "timestamp": None, "severity": 0, "status": 0}
    for key, val in {**defaults, **attrs}.items():
        setattr(v, key, val)
    return v


def _configured_provider() -> ChannelAccessProvider:
    return ChannelAccessProvider(EpicsSettings(EPICS_CA_ADDR_LIST="127.0.0.1"))


async def test_unconfigured_provider_raises_on_get():
    provider = ChannelAccessProvider(EpicsSettings(_env_file=None))
    assert provider.is_configured() is False
    with pytest.raises(EpicsUnconfiguredError):
        await provider.get("some:pv")


async def test_get_returns_pv_value(monkeypatch):
    provider = _configured_provider()

    async def fake_caget(pv_name, format, timeout):
        return _make_value(4.2, severity=0, status=0)

    monkeypatch.setattr(aioca, "caget", fake_caget)

    result = await provider.get("QF12:CURRENT")
    assert result.name == "QF12:CURRENT"
    assert float(result.value) == 4.2


async def test_get_translates_canothing_into_argus_error(monkeypatch):
    provider = _configured_provider()

    async def fake_caget(pv_name, format, timeout):
        raise aioca.CANothing(pv_name, 1)

    monkeypatch.setattr(aioca, "caget", fake_caget)

    with pytest.raises(EpicsConnectionError):
        await provider.get("QF12:CURRENT")


async def test_put_writes_value(monkeypatch):
    provider = _configured_provider()
    seen = {}

    async def fake_caput(pv_name, value, timeout):
        seen["pv_name"] = pv_name
        seen["value"] = value
        return True

    monkeypatch.setattr(aioca, "caput", fake_caput)

    result = await provider.put("QF12:CURRENT", 5.0)
    assert result.accepted is True
    assert seen == {"pv_name": "QF12:CURRENT", "value": 5.0}


async def test_get_many_marks_disconnected_pvs(monkeypatch):
    provider = _configured_provider()

    async def fake_caget(pv_names, format, timeout, throw):
        ok_value = _make_value(1.0)
        bad_value = aioca.CANothing("QF12:BAD", errorcode=200)  # non-ECA_NORMAL -> ok=False
        return [ok_value, bad_value]

    monkeypatch.setattr(aioca, "caget", fake_caget)

    results = await provider.get_many(["QF12:OK", "QF12:BAD"])
    assert results[0].status is None
    assert results[1].status == "disconnected"


@pytest.fixture
def channels(monkeypatch):
    """aioca's cache as this provider sees it: {pv: connected}, and what was dropped."""

    class Cache(dict):
        dropped: list[str] = []

    cache = Cache()
    cache.dropped = []
    monkeypatch.setattr(channel_access, "_channel_state", cache.get)

    def drop(pv):
        cache.dropped.append(pv)
        cache.pop(pv, None)

    monkeypatch.setattr(channel_access, "_drop_channel", drop)
    return cache


def _sequence(monkeypatch, name, *outcomes):
    calls = []

    async def fake(pv_name, *args, **kwargs):
        outcome = outcomes[len(calls)]
        calls.append(pv_name)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(aioca, name, fake)
    return calls


_TIMED_OUT = aioca.CANothing("A:PV", cadef.ECA_TIMEOUT)
_NO_ACCESS = 376  # ECA_NOWTACCESS: cadef names only a few of libca's codes


async def test_a_timeout_on_a_cached_channel_retries_once_on_a_fresh_one(monkeypatch, channels):
    channels["A:PV"] = False
    calls = _sequence(monkeypatch, "caget", _TIMED_OUT, _make_value(-50.85))

    assert (await _configured_provider().get("A:PV")).value == -50.85
    assert calls == ["A:PV", "A:PV"] and channels.dropped == ["A:PV"]


async def test_a_timeout_on_a_new_channel_drops_it_without_retrying(monkeypatch, channels):
    calls = _sequence(monkeypatch, "caget", _TIMED_OUT)

    with pytest.raises(EpicsTimeoutError):
        await _configured_provider().get("A:PV")
    assert calls == ["A:PV"] and channels.dropped == ["A:PV"]


async def test_a_retry_that_also_times_out_says_so_and_leaves_no_channel(monkeypatch, channels):
    channels["A:PV"] = True
    calls = _sequence(monkeypatch, "caget", _TIMED_OUT, _TIMED_OUT)

    with pytest.raises(EpicsTimeoutError, match="Timeout"):
        await _configured_provider().get("A:PV")
    assert len(calls) == 2 and channels.dropped == ["A:PV", "A:PV"]


async def test_another_failure_keeps_the_channel(monkeypatch, channels):
    channels["A:PV"] = True
    _sequence(monkeypatch, "caget", aioca.CANothing("A:PV", _NO_ACCESS))

    with pytest.raises(EpicsConnectionError):
        await _configured_provider().get("A:PV")
    assert channels.dropped == []


async def test_a_write_is_retried_only_if_the_channel_never_connected(monkeypatch, channels):
    channels["A:PV"] = False
    calls = _sequence(monkeypatch, "caput", _TIMED_OUT, None)
    assert (await _configured_provider().put("A:PV", 1.0)).accepted
    assert len(calls) == 2


async def test_a_write_on_a_connected_channel_may_have_landed_so_is_not_retried(monkeypatch, channels):
    channels["A:PV"] = True
    calls = _sequence(monkeypatch, "caput", _TIMED_OUT)
    with pytest.raises(EpicsTimeoutError):
        await _configured_provider().put("A:PV", 1.0)
    assert len(calls) == 1 and channels.dropped == ["A:PV"]


async def test_a_refused_write_is_a_rejection(monkeypatch, channels):
    _sequence(monkeypatch, "caput", aioca.CANothing("A:PV", _NO_ACCESS))
    with pytest.raises(PVWriteRejectedError):
        await _configured_provider().put("A:PV", 1.0)


async def test_a_batch_drops_only_channels_that_timed_out_without_retrying(monkeypatch, channels):
    results = [_make_value(1.0), aioca.CANothing("B", cadef.ECA_DISCONN)]

    async def fake(pv_names, format, timeout, throw):
        return results

    monkeypatch.setattr(aioca, "caget", fake)
    values = await _configured_provider().get_many(["A", "B"])
    assert values[1].status == "disconnected" and channels.dropped == ["B"]


async def test_dropping_a_channel_closes_only_that_one_in_real_aioca(monkeypatch):
    from aioca import _catools

    cache = _catools._Context.get_channel_cache()

    class Fake:
        purged = False

        def _purge(self):
            self.purged = True

    gone, kept = Fake(), Fake()
    cache._ChannelCache__channels.update({"GONE": gone, "KEPT": kept})
    try:
        channel_access._drop_channel("GONE")
        channel_access._drop_channel("NEVER:OPENED")
        assert gone.purged and not kept.purged
        assert list(cache._ChannelCache__channels) == ["KEPT"]
    finally:
        cache._ChannelCache__channels.clear()
