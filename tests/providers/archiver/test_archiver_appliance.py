from datetime import datetime, timedelta

import httpx
import pytest
import respx

from argus.config.settings import ArchiverSettings
from argus.providers.archiver.archiver_appliance import ArchiverApplianceProvider
from argus.providers.archiver.exceptions import ArchiverQueryError, ArchiverUnavailableError, ArchiverUnconfiguredError


def _configured() -> ArchiverApplianceProvider:
    return ArchiverApplianceProvider(ArchiverSettings(_env_file=None, ARCHIVER_BASE_URL="http://archiver.test"))


async def test_unconfigured_raises_clean_error():
    provider = ArchiverApplianceProvider(ArchiverSettings(_env_file=None))
    assert provider.is_configured() is False
    with pytest.raises(ArchiverUnconfiguredError):
        await provider.get_data("QF12:CURRENT", datetime.now() - timedelta(hours=1), datetime.now())


@respx.mock
async def test_get_data_parses_samples():
    provider = _configured()
    respx.get("http://archiver.test/retrieval/data/getData.json").mock(
        return_value=httpx.Response(
            200,
            json=[{"data": [{"secs": 1700000000, "nanos": 0, "val": 4.2, "severity": 0}]}],
        )
    )
    series = await provider.get_data("QF12:CURRENT", datetime.now() - timedelta(hours=1), datetime.now())
    assert series.pv_name == "QF12:CURRENT"
    assert series.samples[0].value == 4.2


@respx.mock
async def test_get_data_translates_connection_error():
    provider = _configured()
    respx.get("http://archiver.test/retrieval/data/getData.json").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ArchiverUnavailableError):
        await provider.get_data("QF12:CURRENT", datetime.now() - timedelta(hours=1), datetime.now())


@respx.mock
async def test_get_data_unexpected_shape_raises_query_error():
    provider = _configured()
    # An empty list response means series[0] raises IndexError -> ArchiverQueryError.
    respx.get("http://archiver.test/retrieval/data/getData.json").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(ArchiverQueryError):
        await provider.get_data("QF12:CURRENT", datetime.now() - timedelta(hours=1), datetime.now())


@respx.mock
async def test_get_data_max_points_uses_optimized_and_parses_bins_and_raw():
    provider = _configured()
    route = respx.get("http://archiver.test/retrieval/data/getData.json").mock(
        return_value=httpx.Response(200, json=[{
            "meta": {"EGU": "A"},
            "data": [
                {"secs": 1700000000, "nanos": 0, "val": [1.5, 0.2, 1.0, 2.0, 10.0]},
                {"secs": 1700000100, "nanos": 0, "val": 3.0, "severity": 0},
            ],
        }])
    )
    series = await provider.get_data("QF12:CURRENT", datetime.now() - timedelta(hours=1), datetime.now(),
                                      max_points=100)
    assert route.calls.last.request.url.params["pv"] == "optimized_100(QF12:CURRENT)"
    assert series.binned and series.units == "A"
    first, second = series.samples
    assert (first.value, first.min, first.max, first.count) == (1.5, 1.0, 2.0, 10)
    assert (second.value, second.count) == (3.0, None)
    assert first.timestamp.tzinfo is not None


@respx.mock
async def test_get_data_raw_waveform_is_not_mistaken_for_a_bin():
    provider = _configured()
    respx.get("http://archiver.test/retrieval/data/getData.json").mock(
        return_value=httpx.Response(200, json=[{"data": [{"secs": 1700000000, "val": [1, 2, 3, 4, 5]}]}])
    )
    series = await provider.get_data("WF", datetime.now() - timedelta(hours=1), datetime.now())
    assert series.samples[0].value == [1, 2, 3, 4, 5] and not series.binned
