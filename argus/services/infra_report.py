"""Infrastructure report on demand: Kubernetes resources, storage, network and logs, now
against their recent trend, and the issues those trends point to.

Code measures, the model explains: every number comes from Prometheus or Loki; the
chat model that asked for the report writes its narrative from these facts. Trends compare
the current value with the average over `days` (Prometheus keeps about 10 days here), and
forecasts extrapolate the last 3 days linearly (days until a disk or volume is full).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

MAX_ROWS = 15
# Deadlines: a chat is waiting. Loki scans every log line in range, and one busy IOC logs
# over a million error lines a day, so the log trend compares the last 24 h with the 24 h
# before (two days scanned, not a week) and gives up after LOKI_DEADLINE.
PROM_DEADLINE = 20.0
LOKI_DEADLINE = 25.0
SEVERITY = {"critical": 0, "warning": 1, "info": 2}


def _gib(b: float | None) -> float | None:
    return None if b is None else round(b / 2**30, 2)


def _pct(x: float | None) -> float | None:
    return None if x is None else round(100 * x, 1)


def _by(rows: list[tuple[dict, float]], *keys: str) -> dict[tuple, float]:
    return {tuple(labels.get(k, "") for k in keys): value for labels, value in rows}


class InfraReportService:
    def __init__(self, prometheus: Any, loki: Any | None, default_namespace: str) -> None:
        self._prom = prometheus
        self._loki = loki
        self._namespace = default_namespace

    async def _q(self, promql: str) -> list[tuple[dict, float]]:
        try:
            return await asyncio.wait_for(self._prom.query(promql), PROM_DEADLINE)
        except Exception:  # noqa: BLE001 - one missing or slow metric must not sink the report
            return []

    async def report(self, *, namespaces: list[str] | None = None, cluster: bool = False,
                     days: int = 7) -> dict[str, Any]:
        days = max(1, min(int(days), 9))
        ns = ".+" if cluster else "|".join(namespaces or [self._namespace])
        sel = f'namespace=~{json.dumps(ns)}'
        w = f"{days}d"
        fs = 'mountpoint="/",fstype!~"tmpfs|overlay|squashfs"'
        q = {
            "node_cpu_now": '1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[10m]))',
            "node_cpu_avg": f'1 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[{w}]))',
            "node_mem_now": "1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes",
            "node_mem_then": f"1 - node_memory_MemAvailable_bytes offset {w} / node_memory_MemTotal_bytes offset {w}",
            "node_fs_used": f"1 - node_filesystem_avail_bytes{{{fs}}} / node_filesystem_size_bytes{{{fs}}}",
            "node_fs_days": f"node_filesystem_avail_bytes{{{fs}}} / -deriv(node_filesystem_avail_bytes{{{fs}}}[3d]) / 86400",
            # Only conditions that mean trouble: distributions add informational ones (EtcdIsVoter...).
            "node_bad": 'kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure|'
                        'NetworkUnavailable",status="true"} == 1 '
                        'or kube_node_status_condition{condition="Ready",status!="true"} == 1',
            "pvc_used": f"kubelet_volume_stats_used_bytes{{{sel}}}",
            "pvc_cap": f"kubelet_volume_stats_capacity_bytes{{{sel}}}",
            "pvc_growth": f"deriv(kubelet_volume_stats_used_bytes{{{sel}}}[3d]) * 86400",
            "ns_cpu_now": f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{{sel},container!=""}}[10m]))',
            "ns_cpu_avg": f'sum by (namespace) (rate(container_cpu_usage_seconds_total{{{sel},container!=""}}[{w}]))',
            "ns_mem_now": f'sum by (namespace) (container_memory_working_set_bytes{{{sel},container!=""}})',
            "ns_mem_avg": f'avg_over_time(sum by (namespace) (container_memory_working_set_bytes{{{sel},container!=""}})[{w}:1h])',
            "ns_rx_now": f"sum by (namespace) (rate(container_network_receive_bytes_total{{{sel}}}[10m]))",
            "ns_rx_avg": f"sum by (namespace) (rate(container_network_receive_bytes_total{{{sel}}}[{w}]))",
            "ns_tx_now": f"sum by (namespace) (rate(container_network_transmit_bytes_total{{{sel}}}[10m]))",
            "ns_tx_avg": f"sum by (namespace) (rate(container_network_transmit_bytes_total{{{sel}}}[{w}]))",
            "restarts": f"sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total{{{sel}}}[24h])) > 0",
            "waiting": f'kube_pod_container_status_waiting_reason{{{sel},reason!="ContainerCreating"}} == 1',
            "not_running": f'sum by (namespace, pod, phase) (kube_pod_status_phase{{{sel},phase=~"Pending|Failed|Unknown"}}) == 1',
            "oom": f"sum by (namespace, pod) (increase(container_oom_events_total{{{sel}}}[24h])) > 0",
            "throttled": f"sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[1h])) "
                         f"/ sum by (namespace, pod) (rate(container_cpu_cfs_periods_total{{{sel}}}[1h])) > 0.25",
            "alerts": 'ALERTS{alertstate="firing",alertname!~"Watchdog|InfoInhibitor"}',
        }
        keys = list(q)
        results = dict(zip(keys, await asyncio.gather(*(self._q(q[k]) for k in keys))))
        issues: list[dict[str, Any]] = []

        def issue(severity: str, what: str, detail: str) -> None:
            issues.append({"severity": severity, "what": what, "detail": detail})

        # nodes
        cpu_now, cpu_avg = _by(results["node_cpu_now"], "instance"), _by(results["node_cpu_avg"], "instance")
        mem_now, mem_then = _by(results["node_mem_now"], "instance"), _by(results["node_mem_then"], "instance")
        fs_used, fs_days = _by(results["node_fs_used"], "instance"), _by(results["node_fs_days"], "instance")
        nodes = []
        for key in sorted(set(cpu_now) | set(mem_now) | set(fs_used)):
            inst = key[0].split(":")[0]
            row = {"node": inst, "cpu_now_pct": _pct(cpu_now.get(key)), "cpu_avg_pct": _pct(cpu_avg.get(key)),
                   "mem_now_pct": _pct(mem_now.get(key)), "mem_then_pct": _pct(mem_then.get(key)),
                   "rootfs_used_pct": _pct(fs_used.get(key))}
            d = fs_days.get(key)
            row["rootfs_days_to_full"] = round(d, 1) if d is not None and 0 < d < 365 else None
            nodes.append(row)
            if row["rootfs_used_pct"] and row["rootfs_used_pct"] > 90:
                issue("critical" if row["rootfs_used_pct"] > 95 else "warning", f"node {inst} root disk",
                      f"{row['rootfs_used_pct']}% used")
            if row["rootfs_days_to_full"] is not None and row["rootfs_days_to_full"] < 14:
                issue("critical" if row["rootfs_days_to_full"] < 3 else "warning", f"node {inst} root disk",
                      f"full in about {row['rootfs_days_to_full']} days at the last 3 days' rate")
            if row["mem_now_pct"] and row["mem_then_pct"] and row["mem_now_pct"] > 80 \
                    and row["mem_now_pct"] - row["mem_then_pct"] > 15:
                issue("warning", f"node {inst} memory", f"{row['mem_now_pct']}% used, up from "
                      f"{row['mem_then_pct']}% {days} days ago")
            if row["cpu_now_pct"] and row["cpu_now_pct"] > 90:
                issue("warning", f"node {inst} CPU", f"{row['cpu_now_pct']}% busy now (average {row['cpu_avg_pct']}%)")
        for labels, _ in results["node_bad"]:
            cond, status = labels.get("condition"), labels.get("status")
            issue("critical", f"node {labels.get('node', '?')}",
                  "not Ready (status unknown: the node stopped reporting)" if cond == "Ready" and status == "unknown"
                  else "not Ready" if cond == "Ready" else f"{cond}")

        # volumes
        used, cap = _by(results["pvc_used"], "namespace", "persistentvolumeclaim"), \
            _by(results["pvc_cap"], "namespace", "persistentvolumeclaim")
        growth = _by(results["pvc_growth"], "namespace", "persistentvolumeclaim")
        # Volumes on one shared filesystem (NFS, CephFS) report the same used and capacity bytes:
        # one row and one issue for the filesystem, naming every claim on it.
        shared: dict[tuple, list[tuple]] = {}
        for key, u in used.items():
            shared.setdefault((u, cap.get(key)), []).append(key)
        volumes = []
        for (u, _c), keys in shared.items():
            key = keys[0]
            c = cap.get(key)
            g = growth.get(key)
            days_left = round((c - u) / g, 1) if c and g and g > 0 else None
            row = {"namespace": key[0], "pvc": key[1], "used_gib": _gib(u), "capacity_gib": _gib(c),
                   **({"shared_with": [f"{k[0]}/{k[1]}" for k in keys[1:]]} if len(keys) > 1 else {}),
                   "used_pct": _pct(u / c) if c else None, "growth_gib_per_day": _gib(g) if g else 0.0,
                   "days_to_full": days_left if days_left is not None and days_left < 365 else None}
            volumes.append(row)
            label = f"volume {key[0]}/{key[1]}" + (f" (shared filesystem, {len(keys)} volumes)" if len(keys) > 1 else "")
            if row["used_pct"] and row["used_pct"] > 85:
                issue("critical" if row["used_pct"] > 95 else "warning", label,
                      f"{row['used_pct']}% used ({row['used_gib']} of {row['capacity_gib']} GiB)")
            if row["days_to_full"] is not None and row["days_to_full"] < 14:
                issue("critical" if row["days_to_full"] < 3 else "warning", label,
                      f"full in about {row['days_to_full']} days (+{row['growth_gib_per_day']} GiB/day)")
        volumes.sort(key=lambda r: -(r["used_pct"] or 0))

        # namespaces
        def pair(now: str, avg: str) -> dict[str, tuple[float | None, float | None]]:
            a, b = _by(results[now], "namespace"), _by(results[avg], "namespace")
            return {k[0]: (a.get(k), b.get(k)) for k in set(a) | set(b)}

        cpu, mem = pair("ns_cpu_now", "ns_cpu_avg"), pair("ns_mem_now", "ns_mem_avg")
        rx, tx = pair("ns_rx_now", "ns_rx_avg"), pair("ns_tx_now", "ns_tx_avg")
        names = sorted(set(cpu) | set(mem), key=lambda n: -((mem.get(n) or (0, 0))[0] or 0))
        spaces = []
        for n in names:
            (cn, ca), (mn, ma) = cpu.get(n, (None, None)), mem.get(n, (None, None))
            (rn, ra), (tn, ta) = rx.get(n, (None, None)), tx.get(n, (None, None))
            spaces.append({"namespace": n, "cpu_cores_now": round(cn, 2) if cn is not None else None,
                           "cpu_cores_avg": round(ca, 2) if ca is not None else None,
                           "memory_gib_now": _gib(mn), "memory_gib_avg": _gib(ma),
                           "rx_mib_s_now": round(rn / 2**20, 2) if rn is not None else None,
                           "rx_mib_s_avg": round(ra / 2**20, 2) if ra is not None else None,
                           "tx_mib_s_now": round(tn / 2**20, 2) if tn is not None else None,
                           "tx_mib_s_avg": round(ta / 2**20, 2) if ta is not None else None})
            if mn and ma and mn > 1.5 * ma and mn > 2**30:
                issue("warning", f"namespace {n} memory",
                      f"{_gib(mn)} GiB now, {round(mn / ma, 1)}x its {days}-day average ({_gib(ma)} GiB)")
            if rn and ra and rn > 3 * ra and rn > 10 * 2**20:
                issue("info", f"namespace {n} network",
                      f"receiving {round(rn / 2**20, 1)} MiB/s, {round(rn / ra, 1)}x its {days}-day average")

        # pods
        pods = {"restarts_24h": sorted(({"namespace": l.get("namespace"), "pod": l.get("pod"), "restarts": round(v)}
                                        for l, v in results["restarts"]), key=lambda r: -r["restarts"]),
                "waiting": [{"namespace": l.get("namespace"), "pod": l.get("pod"), "container": l.get("container"),
                             "reason": l.get("reason")} for l, _ in results["waiting"]],
                "not_running": [{"namespace": l.get("namespace"), "pod": l.get("pod"), "phase": l.get("phase")}
                                for l, _ in results["not_running"]],
                "oom_killed_24h": [{"namespace": l.get("namespace"), "pod": l.get("pod"), "events": round(v)}
                                   for l, v in results["oom"]],
                "cpu_throttled": [{"namespace": l.get("namespace"), "pod": l.get("pod"), "throttled_pct": _pct(v)}
                                  for l, v in results["throttled"]]}
        for r in pods["waiting"]:
            sev = "critical" if r["reason"] in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull") else "warning"
            issue(sev, f"pod {r['namespace']}/{r['pod']}", f"{r['container']} waiting: {r['reason']}")
        for r in pods["restarts_24h"]:
            if r["restarts"] >= 3:
                issue("warning", f"pod {r['namespace']}/{r['pod']}", f"{r['restarts']} restarts in 24 h")
        for r in pods["oom_killed_24h"]:
            issue("warning", f"pod {r['namespace']}/{r['pod']}", f"out of memory {r['events']} time(s) in 24 h")
        for r in pods["not_running"]:
            issue("warning", f"pod {r['namespace']}/{r['pod']}", f"phase {r['phase']}")

        wanted = None if cluster else set((namespaces or [self._namespace]))
        alerts = [{"alert": l.get("alertname"), "severity": l.get("severity"), "namespace": l.get("namespace"),
                   "pod": l.get("pod")} for l, _ in results["alerts"]
                  if wanted is None or not l.get("namespace") or l.get("namespace") in wanted]
        for a in alerts:
            if a["severity"] in ("critical", "warning"):
                issue(a["severity"], f"alert {a['alert']}", f"firing ({a.get('namespace') or 'cluster'})")

        # Counting error lines across every namespace takes Loki over 2 minutes here; one namespace takes seconds.
        logs = {"skipped": "log error counts are per namespace (cluster-wide counting takes Loki minutes): "
                           "ask about one namespace"} if cluster else await self._log_errors(ns, days)
        for r in logs.get("containers", []):
            if r["errors_24h"] >= 100 and r["errors_24h"] > 3 * max(r["errors_previous_24h"], 1):
                issue("warning", f"logs {r['namespace']}/{r['container']}",
                      f"{r['errors_24h']} error lines in 24 h, {r['errors_previous_24h']} the day before")

        issues.sort(key=lambda i: SEVERITY.get(i["severity"], 3))
        return {
            "scope": "cluster" if cluster else sorted(wanted or []), "trend_days": days,
            "issues": issues,
            "nodes": nodes, "volumes": volumes[:MAX_ROWS * 2], "namespaces": spaces[:MAX_ROWS],
            "pods": {k: v[:MAX_ROWS] for k, v in pods.items()}, "alerts": alerts[:MAX_ROWS * 2], "logs": logs,
            "notes": "Trends compare now with the average over trend_days (log errors: the last 24 h with the "
                     "24 h before); days_to_full extrapolates the last 3 days. Summarise the issues first, "
                     "worst first, then what the trends show.",
        }

    async def _log_errors(self, ns: str, days: int) -> dict[str, Any]:  # noqa: ARG002 - days: see LOKI_DEADLINE
        if self._loki is None or not self._loki.is_configured():
            return {"unavailable": "Loki is not configured"}
        pattern = '|~ "(?i)(error|exception|fatal|panic)"'
        sel = f"{{namespace=~{json.dumps(ns)}}}"
        count = f"sum by (namespace, container) (count_over_time({sel} {pattern} [24h]"
        try:
            now, past = await asyncio.wait_for(asyncio.gather(
                self._loki.metric(count + "))", timeout=LOKI_DEADLINE),
                self._loki.metric(count + " offset 1d))", timeout=LOKI_DEADLINE)), LOKI_DEADLINE)
        except asyncio.TimeoutError:
            return {"unavailable": f"Loki did not answer within {LOKI_DEADLINE:.0f} s (too many log lines in "
                                   "range); ask for one namespace instead of the cluster"}
        except Exception as exc:  # noqa: BLE001
            return {"unavailable": str(exc)[:200]}
        base = _by(past, "namespace", "container")
        rows = [{"namespace": l.get("namespace"), "container": l.get("container"), "errors_24h": round(v),
                 "errors_previous_24h": round(base.get((l.get("namespace", ""), l.get("container", "")), 0))}
                for l, v in now]
        rows.sort(key=lambda r: -r["errors_24h"])
        return {"containers": rows[:MAX_ROWS]}
