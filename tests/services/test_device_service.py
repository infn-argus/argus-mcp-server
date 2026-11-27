from argus.core.cache import AsyncTTLCache
from argus.providers.channelfinder.models import CFChannel
from argus.services.device_service import DeviceService


class _UnconfiguredChannelFinder:
    def is_configured(self) -> bool:
        return False


class _ConfiguredChannelFinder:
    def is_configured(self) -> bool:
        return True

    async def find_channels(self, **kwargs):
        return [
            CFChannel(
                name="QF12:CURRENT",
                owner="ops",
                properties={"device": "QF12", "iocName": "ioc-qf12", "rack": "R3"},
                tags=["magnet"],
            ),
            CFChannel(
                name="QF12:STATUS",
                owner="ops",
                properties={"device": "QF12", "iocName": "ioc-qf12"},
                tags=["magnet"],
            ),
        ]


async def test_resolve_device_falls_back_to_bare_pv_when_unconfigured():
    service = DeviceService(channelfinder=_UnconfiguredChannelFinder(), cache=AsyncTTLCache(ttl=60))
    device = await service.resolve_device("QF12:CURRENT")
    assert device.name == "QF12:CURRENT"
    assert device.pv_names == ["QF12:CURRENT"]
    assert device.ioc_name is None


async def test_resolve_device_merges_channel_metadata():
    service = DeviceService(channelfinder=_ConfiguredChannelFinder(), cache=AsyncTTLCache(ttl=60))
    device = await service.resolve_device("QF12")
    assert set(device.pv_names) == {"QF12:CURRENT", "QF12:STATUS"}
    assert device.ioc_name == "ioc-qf12"
    assert device.rack == "R3"
    assert device.owner == "ops"
    assert device.tags == ["magnet"]


async def test_resolve_device_is_cached():
    channelfinder = _ConfiguredChannelFinder()
    calls = 0
    original = channelfinder.find_channels

    async def counting_find_channels(**kwargs):
        nonlocal calls
        calls += 1
        return await original(**kwargs)

    channelfinder.find_channels = counting_find_channels
    service = DeviceService(channelfinder=channelfinder, cache=AsyncTTLCache(ttl=60))

    await service.resolve_device("QF12")
    await service.resolve_device("QF12")
    assert calls == 1


# --- names models actually pass: device part, IOC, pod; unknown names ------

import pytest  # noqa: E402

from argus.services.device_service import DeviceNotFoundError  # noqa: E402


def _cam_channel(pv, device="EUAPS:CAM:FI1-CAM-01", ioc="fi1-cam-vis-01"):
    return CFChannel(name=pv, owner="cf", properties={"device": device, "iocName": ioc}, tags=[])


class _EuapsChannelFinder:
    """Answers like the EUAPS ChannelFinder: `device` is the full prefix."""

    def __init__(self):
        self.channels = [
            _cam_channel("EUAPS:CAM:FI1-CAM-01:ArrayRate_RBV"),
            _cam_channel("EUAPS:CAM:FI1-CAM-01:Acquire_RBV"),
            _cam_channel("EUAPS:CAM:FI8-CAM-01:ArrayRate_RBV", "EUAPS:CAM:FI8-CAM-01", "fi8-cam-vis-001"),
            _cam_channel("EUAPS:MAG:PS1:CURRENT_RB", "EUAPS:MAG:PS1", "psioc"),
            _cam_channel("EUAPS:MAG:PS2:CURRENT_RB", "EUAPS:MAG:PS2", "psioc"),
        ]
        self.queries = []

    def is_configured(self):
        return True

    async def find_channels(self, query=None, **props):
        self.queries.append(props)
        [(key, pattern)] = props.items()
        if pattern.startswith("*"):
            return [c for c in self.channels if c.properties.get(key, "").endswith(pattern[1:])]
        return [c for c in self.channels if c.properties.get(key) == pattern]

    async def get_channel(self, name):
        return next((c for c in self.channels if c.name == name), None)


def _svc(cf=None):
    return DeviceService(channelfinder=cf or _EuapsChannelFinder(), cache=AsyncTTLCache(ttl=60))


async def test_device_part_of_the_name_finds_the_full_device():
    device = await _svc().resolve_device("FI1-CAM-01")
    assert device.name == "EUAPS:CAM:FI1-CAM-01"
    assert device.ioc_name == "fi1-cam-vis-01"
    assert device.primary_pv == "EUAPS:CAM:FI1-CAM-01:ArrayRate_RBV"


async def test_pod_name_of_a_single_device_ioc_finds_its_device():
    # list_iocs shows pods; models pass fi1-cam-vis-01-0 as a device name.
    device = await _svc().resolve_device("fi1-cam-vis-01-0")
    assert device.name == "EUAPS:CAM:FI1-CAM-01"


async def test_ioc_with_several_devices_says_so_and_lists_them():
    with pytest.raises(DeviceNotFoundError, match="IOC 'psioc', not a device.*EUAPS:MAG:PS1, EUAPS:MAG:PS2"):
        await _svc().resolve_device("psioc")


async def test_unknown_name_is_an_error_not_a_made_up_pv():
    with pytest.raises(DeviceNotFoundError) as exc:
        await _svc().resolve_device("no-such-thing")
    assert exc.value.code == "device_not_found"
    assert "list_beamline_devices" in str(exc.value)


async def test_pv_name_not_in_channelfinder_is_still_read_as_given():
    device = await _svc().resolve_device("EUAPS:OTHER:PV")
    assert device.pv_names == ["EUAPS:OTHER:PV"]


async def test_unconfigured_channelfinder_refuses_a_non_pv_name():
    service = DeviceService(channelfinder=_UnconfiguredChannelFinder(), cache=AsyncTTLCache(ttl=60))
    with pytest.raises(DeviceNotFoundError, match="not a PV name"):
        await service.resolve_device("fi1-cam-vis-01-0")


def test_status_view_reads_only_status_pvs_of_a_large_device():
    from argus.services.diagnostics_service import _status_view
    from argus.services.models import Device

    prefix = "EUAPS:CAM:FI1-CAM-01:"
    pvs = [f"{prefix}GC_Feature{i}_RBV" for i in range(500)] + [
        f"{prefix}Overlay1:{i}:Use_RBV" for i in range(1, 9)
    ] + [prefix + s for s in ("ArrayRate_RBV", "Acquire_RBV", "DetectorState_RBV", "StatusMessage_RBV",
                              "AcquirePeriod", "Gain")]
    device = Device(name="EUAPS:CAM:FI1-CAM-01", pv_names=pvs, primary_pv=prefix + "ArrayRate_RBV")
    view, total, note = _status_view(device)
    assert total == len(pvs) and len(view.pv_names) == 30 and "get_pv" in note
    for wanted in ("ArrayRate_RBV", "Acquire_RBV", "DetectorState_RBV", "StatusMessage_RBV"):
        assert prefix + wanted in view.pv_names
    small = Device(name="QF12", pv_names=["QF12:CURRENT"], primary_pv="QF12:CURRENT")
    assert _status_view(small) == (small, None, None)
