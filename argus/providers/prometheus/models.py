from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InterfaceTraffic:
    """Rates over the query window for one network interface (per second)."""

    device: str
    rx_bits_per_s: float = 0.0
    tx_bits_per_s: float = 0.0
    rx_packets_per_s: float = 0.0
    rx_drops_per_s: float = 0.0
    rx_errors_per_s: float = 0.0
    tx_drops_per_s: float = 0.0
    tx_errors_per_s: float = 0.0
    # Node interfaces only (node-exporter); a pod's interfaces have no speed.
    speed_bits_per_s: float | None = None
    mtu: int | None = None


@dataclass
class NodeNetworkStats:
    """Network health of one node, from node-exporter, over the query window."""

    node: str
    instance: str
    window_minutes: int
    interfaces: list[InterfaceTraffic] = field(default_factory=list)
    # Datagrams the kernel threw away because a socket's receive buffer was full:
    # a receiver too slow, or net.core.rmem_max too small, for a UDP stream.
    udp_rcvbuf_errors_per_s: float = 0.0
    udp_in_errors_per_s: float = 0.0
    # Packets dropped before any socket saw them (netdev_max_backlog full), and
    # times the softirq ran out of budget with packets still queued.
    softnet_dropped_per_s: float = 0.0
    softnet_squeezed_per_s: float = 0.0


@dataclass
class NodeCpuStats:
    node: str
    window_minutes: int
    cpus: int
    busy_percent: float
    softirq_percent: float
    iowait_percent: float
    # The busiest single CPU: packet processing often pins one core while the
    # node average looks idle.
    max_cpu_busy_percent: float
    max_cpu_softirq_percent: float
    load1: float | None = None
    memory_available_percent: float | None = None


@dataclass
class PodNetworkStats:
    """A pod's interfaces (eth0, and net1... for extra Multus networks), from cAdvisor."""

    namespace: str
    pod: str
    window_minutes: int
    node: str | None = None
    interfaces: list[InterfaceTraffic] = field(default_factory=list)
