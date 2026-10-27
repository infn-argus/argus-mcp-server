"""Per-device-type domain knowledge: how to classify a device's function
from its name/IOC prefix, and which PV suffixes carry its key setpoint/
readback values, all keyed by `devgroup` (mag/vac/mot/cool/...).

One module per device type (magnet.py, vacuum.py, motor.py, ...) so adding a
new type, or adding more context to an existing one, never touches the
others -- a device abstraction layer, not one large table.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DevfuncRule:
    substrings: tuple[str, ...]
    devfunc: str
    # Only "mot"'s MIR rule matches against iocprefix as well as the device
    # name in the source convention (epik8s-btf/opi/epik8s-opi/Scripts/
    # epik8sutil.py) -- every other rule matches the device name only.
    check_iocprefix: bool = False


@dataclass(frozen=True)
class DeviceTypeProfile:
    devgroup: str
    devfunc_rules: tuple[DevfuncRule, ...] = ()
    devfunc_fallback: str | None = None
    # role name (e.g. "current_readback") -> PV suffix (e.g. "CURRENT_RB").
    # Actual PV name is f"{iocprefix}:{device_name}:{suffix}".
    key_pv_suffixes: dict[str, str] = field(default_factory=dict)

    def derive_devfunc(self, name: str, iocprefix: str) -> str | None:
        for rule in self.devfunc_rules:
            haystacks = (name, iocprefix) if rule.check_iocprefix else (name,)
            if any(s in h for s in rule.substrings for h in haystacks):
                return rule.devfunc
        return self.devfunc_fallback

    def key_pvs(self, iocprefix: str, device_name: str) -> dict[str, str]:
        if not iocprefix:
            return {}
        return self.key_pvs_for_prefix(f"{iocprefix}:{device_name}")

    def key_pvs_for_prefix(self, pv_prefix: str | None) -> dict[str, str]:
        """Key PVs from a device's full PV prefix, for sources (the Knowledge
        Hub) that store the composed prefix rather than iocprefix + name."""
        if not pv_prefix or not self.key_pv_suffixes:
            return {}
        return {role: f"{pv_prefix}:{suffix}" for role, suffix in self.key_pv_suffixes.items()}
