"""Command line for the daily report, run inside a beamline's ARGUS pod.

    python -m argus.report_cli coverage            # PVs the report needs: archived or not (read-only)
    python -m argus.report_cli coverage --archive  # also ask the appliance to archive what is missing
    python -m argus.report_cli daily [--day YYYY-MM-DD] [--out DIR] [--post] [--logbook TEST] [--force]

`daily` computes the day's facts from the archiver (yesterday by default, UTC) and writes
facts.json and report.md to --out. With --post it also posts the report to Olog as the
robot account in ARGUS_REPORT_LOGBOOK_USERNAME / ARGUS_REPORT_LOGBOOK_PASSWORD (the Secret
argus-olog-report), never as the server's own account or a chat user. A day left out of
the description's schedule.days (e.g. weekends on SPARC) is skipped unless --force.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from argus.core.context import AppContext
from argus.providers.logbook.logbook import LogbookProvider
from argus.services.daily_report import DailyReport, day_window, render, report_wanted
from argus.services.report_service import resolve


async def _coverage(ctx: AppContext, args: argparse.Namespace) -> dict:
    service = ctx.report_service
    assert service is not None
    if args.archive:
        return await service.archive_missing(args.repo, args.ref)
    resolution = await service.resolution(args.repo, args.ref)
    coverage = await service.coverage(args.repo, args.ref)
    return {"beamline": resolution.beamline, "devices_per_group": resolution.counts(),
            "devices_without_function": resolution.no_function, "devices_in_no_path": resolution.no_path,
            "archiver": coverage.to_dict(limit=args.limit)}


def _robot_logbook(ctx: AppContext) -> LogbookProvider:
    user = os.environ.get("ARGUS_REPORT_LOGBOOK_USERNAME")
    password = os.environ.get("ARGUS_REPORT_LOGBOOK_PASSWORD")
    if not (user and password):
        raise SystemExit("--post needs ARGUS_REPORT_LOGBOOK_USERNAME and ARGUS_REPORT_LOGBOOK_PASSWORD "
                         "(the Secret argus-olog-report).")
    return LogbookProvider(ctx.settings.logbook.model_copy(update={"username": user, "password": password}))


async def _daily(ctx: AppContext, args: argparse.Namespace) -> dict:
    service = ctx.report_service
    assert service is not None
    config, devices = await service.load(args.repo, args.ref)
    resolution = resolve(config, devices)
    start, end = day_window(args.day)
    if not args.force and not report_wanted(config, start):
        return {"skipped": start.strftime("%Y-%m-%d (%A)"),
                "reason": f"not in schedule.days {config['schedule']['days']} of argus-report.yaml"}
    # Olog refuses anonymous searches: read the day's entries as the robot account when it is set.
    robot = _robot_logbook(ctx) if os.environ.get("ARGUS_REPORT_LOGBOOK_USERNAME") else None
    report = DailyReport(ctx.archiver, ctx.operations_service.pv_names_matching, logbook=robot or ctx.logbook)
    facts = await report.facts(config, resolution, start, end)
    title, body = render(facts)
    out = Path(args.out) / f"{facts['beamline'].lower()}-{facts['start'][:10]}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "facts.json").write_text(json.dumps(facts, indent=1, default=str))
    (out / "report.md").write_text(f"# {title}\n\n{body}")
    result = {"title": title, "written": str(out), "pvs_read": facts["pvs_read"],
              "pvs_without_history": facts["pvs_without_history"]}
    if args.post:
        logbook = args.logbook or (config.get("logbooks") or {}).get("report") or "TEST"
        entry = await (robot or _robot_logbook(ctx)).create_entry(title, body, [logbook], tags=[])
        result["posted"] = {"id": entry.id, "logbook": logbook, "author": entry.author}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m argus.report_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    cov = sub.add_parser("coverage", help="PVs the daily report needs, and which are archived")
    cov.add_argument("--archive", action="store_true", help="archive the missing PVs that answer")
    cov.add_argument("--limit", type=int, default=50, help="names listed per category")
    daily = sub.add_parser("daily", help="compute the day's facts and render the report")
    daily.add_argument("--day", default=None, help="YYYY-MM-DD (UTC); default yesterday")
    daily.add_argument("--out", default="/tmp/argus-report", help="directory for facts.json and report.md")
    daily.add_argument("--post", action="store_true", help="post the report to Olog as the robot account")
    daily.add_argument("--logbook", default=None, help="logbook to post to; default the config's logbooks.report")
    daily.add_argument("--force", action="store_true", help="report even on a day schedule.days leaves out")
    for p in (cov, daily):
        p.add_argument("--repo", default=None)
        p.add_argument("--ref", default=None)
    args = parser.parse_args()
    ctx = AppContext.build()
    run = _coverage if args.command == "coverage" else _daily
    print(json.dumps(asyncio.run(run(ctx, args)), indent=2, default=str))


if __name__ == "__main__":
    main()
