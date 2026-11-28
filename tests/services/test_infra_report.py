from __future__ import annotations

from argus.services.infra_report import InfraReportService


class Prom:
    """Answers by the first matching substring of the query."""

    def __init__(self, answers):
        self.answers = answers
        self.queries = []

    async def query(self, promql):
        self.queries.append(promql)
        for key, rows in self.answers.items():
            if key in promql:
                return rows
        return []


class Loki:
    def is_configured(self):
        return True

    async def metric(self, logql):
        if "[24h]" in logql:
            return [({"namespace": "sparc", "container": "ioc"}, 900.0)]
        return [({"namespace": "sparc", "container": "ioc"}, 700.0)]          # 100 a day over 7 days


GIB = 2**30


async def test_trends_become_issues():
    prom = Prom({
        "node_filesystem_avail_bytes{mountpoint=\"/\",fstype!~\"tmpfs|overlay|squashfs\"} / -deriv":
            [({"instance": "n1:9100"}, 5.0)],
        "1 - node_filesystem_avail_bytes": [({"instance": "n1:9100"}, 0.80)],
        "kubelet_volume_stats_used_bytes{namespace=~\"sparc\"}[3d]": [({"namespace": "sparc", "persistentvolumeclaim": "data"}, 2 * GIB)],
        "kubelet_volume_stats_used_bytes": [({"namespace": "sparc", "persistentvolumeclaim": "data"}, 90 * GIB)],
        "kubelet_volume_stats_capacity_bytes": [({"namespace": "sparc", "persistentvolumeclaim": "data"}, 100 * GIB)],
        "kube_pod_container_status_waiting_reason": [({"namespace": "sparc", "pod": "cam-0", "container": "ioc",
                                                       "reason": "CrashLoopBackOff"}, 1.0)],
        "kube_pod_container_status_restarts_total": [({"namespace": "sparc", "pod": "cam-0"}, 12.0)],
    })
    report = await InfraReportService(prom, Loki(), "sparc").report()
    what = [(i["severity"], i["what"]) for i in report["issues"]]
    assert what[0] == ("critical", "pod sparc/cam-0")                       # worst first
    assert ("warning", "node n1 root disk") in what                         # full in ~5 days
    assert ("critical", "volume sparc/data") in what or ("warning", "volume sparc/data") in what
    vol = report["volumes"][0]
    assert (vol["used_pct"], vol["days_to_full"], vol["growth_gib_per_day"]) == (90.0, 5.0, 2.0)
    assert ("warning", "logs sparc/ioc") in what                            # 900 vs 100 a day
    assert report["scope"] == ["sparc"]
    assert all('namespace=~"sparc"' in q for q in prom.queries if "kubelet_volume" in q)


async def test_cluster_scope_and_missing_metrics_do_not_fail():
    report = await InfraReportService(Prom({}), None, "sparc").report(cluster=True, days=30)
    assert report["scope"] == "cluster" and report["trend_days"] == 9 and report["issues"] == []
    assert report["logs"] == {"unavailable": "Loki is not configured"}


async def test_only_trouble_conditions_are_issues():
    prom = Prom({"kube_node_status_condition": [({"node": "n2", "condition": "Ready", "status": "unknown"}, 1.0)]})
    report = await InfraReportService(prom, None, "sparc").report()
    assert [(i["what"], i["detail"]) for i in report["issues"]] == [
        ("node n2", "not Ready (status unknown: the node stopped reporting)")]
    assert "EtcdIsVoter" not in next(q for q in prom.queries if "kube_node_status_condition" in q)


async def test_volumes_on_one_shared_filesystem_are_one_row():
    rows = lambda v: [({"namespace": ns, "persistentvolumeclaim": c}, v) for ns, c in (("a", "x"), ("b", "y"))]
    prom = Prom({"[3d]) * 86400": rows(10 * GIB), "kubelet_volume_stats_used_bytes": rows(50 * GIB),
                 "kubelet_volume_stats_capacity_bytes": rows(100 * GIB)})
    report = await InfraReportService(prom, None, "a").report(cluster=True)
    assert len(report["volumes"]) == 1 and report["volumes"][0]["shared_with"] == ["b/y"]
    assert [i["what"] for i in report["issues"]] == ["volume a/x (shared filesystem, 2 volumes)"]
