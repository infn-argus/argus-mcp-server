"""Command line for the daily report's archive coverage, run inside a beamline's ARGUS pod.

    python -m argus.report_cli coverage            # what is archived, what is not (read-only)
    python -m argus.report_cli coverage --archive  # also ask the appliance to archive what is missing
"""

from __future__ import annotations

import argparse
import asyncio
import json

from argus.core.context import AppContext


async def _run(args: argparse.Namespace) -> dict:
    ctx = AppContext.build()
    service = ctx.report_service
    assert service is not None
    if args.archive:
        return await service.archive_missing(args.repo, args.ref)
    resolution = await service.resolution(args.repo, args.ref)
    coverage = await service.coverage(args.repo, args.ref)
    return {"beamline": resolution.beamline, "devices_per_group": resolution.counts(),
            "devices_without_function": resolution.no_function, "devices_in_no_path": resolution.no_path,
            "archiver": coverage.to_dict(limit=args.limit)}


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m argus.report_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    cov = sub.add_parser("coverage", help="PVs the daily report needs, and which are archived")
    cov.add_argument("--archive", action="store_true", help="archive the missing PVs that answer")
    cov.add_argument("--repo", default=None)
    cov.add_argument("--ref", default=None)
    cov.add_argument("--limit", type=int, default=50, help="names listed per category")
    print(json.dumps(asyncio.run(_run(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()
