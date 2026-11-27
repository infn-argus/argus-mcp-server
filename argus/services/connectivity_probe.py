"""Reachability probe for network devices: ping, TCP port, GigE Vision control.

Standard library only and self-contained on purpose: the same source runs in
two places. ARGUS imports it and calls ``probe()`` in its own pod; and the
connectivity service sends this file's source to an IOC pod
(``python3 -c <source> <json>``) so the identical checks run inside the IOC's
own network namespace - its VLAN attachments, routes and firewall position.

Read-only by construction:
- ICMP echo through an unprivileged "ping socket" (no raw sockets, no
  capabilities; needs net.ipv4.ping_group_range to include the uid, which is
  the Linux default on current kernels), falling back to the ``ping`` binary.
- TCP: connect, then close. Nothing is sent.
- GigE Vision: one GVCP READREG of the Control Channel Privilege register
  (0x0A00) - the register every GigE camera answers, and the same read the
  camera's own controller makes each second. Says whether the camera answers
  and whether some application currently controls it.

Output is a JSON-friendly dict; as a script it prints that dict as JSON.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PROBE_VERSION = 1
MAX_TARGETS = 32
MAX_COUNT = 5

_GVCP_PORT = 3956
_GVCP_CCP_REGISTER = 0x0A00
# CCP bits (GigE Vision 2.x, register 0x0A00): exclusive, control, switchover.
_CCP_EXCLUSIVE = 0x1
_CCP_CONTROL = 0x2

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")


def _resolve(host: str) -> tuple[str | None, str | None]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return None, f"DNS lookup failed: {exc.strerror or exc}"
    return (infos[0][4][0], None) if infos else (None, "DNS lookup returned no IPv4 address")


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _ping_socket(ip: str, count: int, timeout: float) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    sock.settimeout(timeout)
    rtts: list[float] = []
    try:
        for seq in range(count):
            # The kernel rewrites the identifier for ping sockets; the checksum
            # is filled in anyway so the packet is valid on any kernel.
            header = struct.pack("!BBHHH", 8, 0, 0, 0, seq)
            payload = b"argus-probe"
            packet = struct.pack("!BBHHH", 8, 0, _checksum(header + payload), 0, seq) + payload
            start = time.monotonic()
            sock.sendto(packet, (ip, 0))
            deadline = start + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    reply, _ = sock.recvfrom(1024)
                except socket.timeout:
                    break
                # A ping socket returns the ICMP message without the IP header.
                if len(reply) >= 8 and reply[0] == 0 and struct.unpack("!H", reply[6:8])[0] == seq:
                    rtts.append((time.monotonic() - start) * 1000)
                    break
    finally:
        sock.close()
    return _ping_result("ping-socket", count, rtts)


def _ping_binary(ip: str, count: int, timeout: float) -> dict:
    binary = shutil.which("ping")
    if not binary:
        return {"method": None, "error": "no ping socket permission and no ping binary"}
    try:
        out = subprocess.run(
            [binary, "-n", "-c", str(count), "-W", str(max(1, int(timeout))), ip],
            capture_output=True, text=True, timeout=count * (timeout + 1) + 2,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"method": "ping-binary", "error": str(exc)}
    rtts = [float(m) for m in re.findall(r"time[=<]([\d.]+)\s*ms", out)]
    return _ping_result("ping-binary", count, rtts)


def _ping_result(method: str, count: int, rtts: list[float]) -> dict:
    result: dict = {"method": method, "sent": count, "received": len(rtts)}
    if rtts:
        result["rtt_ms_min"] = round(min(rtts), 2)
        result["rtt_ms_avg"] = round(sum(rtts) / len(rtts), 2)
        result["rtt_ms_max"] = round(max(rtts), 2)
    return result


def _ping(ip: str, count: int, timeout: float) -> dict:
    try:
        return _ping_socket(ip, count, timeout)
    except PermissionError:
        return _ping_binary(ip, count, timeout)
    except OSError as exc:
        # e.g. EPERM wrapped differently, or ICMP not supported in this namespace.
        if getattr(exc, "errno", None) in (1, 13, 93, 97):
            return _ping_binary(ip, count, timeout)
        return {"method": "ping-socket", "error": str(exc)}


def _tcp(ip: str, port: int, timeout: float) -> dict:
    start = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return {"port": port, "state": "open", "connect_ms": round((time.monotonic() - start) * 1000, 2)}
    except ConnectionRefusedError:
        return {"port": port, "state": "refused"}
    except socket.timeout:
        return {"port": port, "state": "timeout"}
    except OSError as exc:
        return {"port": port, "state": "error", "error": str(exc)}


def _gvcp_ccp(ip: str, timeout: float) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    req_id = os.getpid() & 0xFFFF or 1
    # GVCP command header: key 0x42, flags 0x01 (ack required), READREG_CMD
    # 0x0080, payload length 4, request id; then one 32-bit register address.
    packet = struct.pack("!BBHHHI", 0x42, 0x01, 0x0080, 4, req_id, _GVCP_CCP_REGISTER)
    try:
        for _ in range(2):
            sock.sendto(packet, (ip, _GVCP_PORT))
            try:
                reply, _ = sock.recvfrom(64)
            except socket.timeout:
                continue
            if len(reply) >= 12 and struct.unpack("!H", reply[2:4])[0] == 0x0081:
                status = struct.unpack("!H", reply[0:2])[0]
                if status != 0:
                    return {"answered": True, "status": f"0x{status:04x}"}
                value = struct.unpack("!I", reply[8:12])[0]
                controlled = bool(value & (_CCP_CONTROL | _CCP_EXCLUSIVE))
                return {"answered": True, "ccp": f"0x{value:x}", "controlled": controlled}
        return {"answered": False}
    except OSError as exc:
        return {"answered": False, "error": str(exc)}
    finally:
        sock.close()


def _probe_one(target: dict, count: int, timeout: float) -> dict:
    host = str(target.get("host", ""))
    result: dict = {"host": host}
    if not _HOST_RE.match(host):
        result["error"] = "invalid host"
        return result
    ip, error = _resolve(host)
    result["ip"] = ip
    if error:
        result["error"] = error
        return result
    result["ping"] = _ping(ip, count, timeout)
    port = target.get("port")
    if isinstance(port, int) and 0 < port < 65536:
        result["tcp"] = _tcp(ip, port, timeout * 2)
    if target.get("kind") == "gige":
        result["gvcp"] = _gvcp_ccp(ip, timeout)
    return result


def probe(targets: list[dict], count: int = 3, timeout: float = 1.0) -> dict:
    """Probe every target concurrently; one dict per target, in input order."""
    count = max(1, min(MAX_COUNT, int(count)))
    timeout = max(0.2, min(3.0, float(timeout)))
    targets = list(targets)[:MAX_TARGETS]
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(targets)))) as pool:
        results = list(pool.map(lambda t: _probe_one(t, count, timeout), targets))
    return {"probe_version": PROBE_VERSION, "hostname": socket.gethostname(), "results": results}


if __name__ == "__main__":
    request = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    print(json.dumps(probe(request.get("targets", []), request.get("count", 3), request.get("timeout", 1.0))))
