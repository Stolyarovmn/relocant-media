import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from relocant_media.scheduler.config import load_city_operations
from relocant_media.scheduler.metrics import UnavailablePostMetricsProvider
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.scheduler.runner import run_scheduler_tick
from relocant_media.storage.db import SessionFactory


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic Relocant Media scheduled work")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tick = subparsers.add_parser("tick", help="Run all due non-publishing operational tasks")
    tick.add_argument("--city-config", type=Path, default=Path("cities/belgrade/city.yaml"))
    tick.add_argument("--sources", type=Path, default=Path("cities/belgrade/sources"))
    tick.add_argument("--now", type=_aware_datetime)

    status = subparsers.add_parser("status", help="Show recent scheduler audit rows")
    status.add_argument("--city", default="belgrade")
    status.add_argument("--limit", type=int, default=25)
    return parser


async def _tick(args: argparse.Namespace) -> dict[str, Any]:
    now = args.now or datetime.now(timezone.utc)
    city_config = load_city_operations(args.city_config)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": "relocant-media/0.1 (+scheduled-source-monitoring)"},
    ) as client:
        with SessionFactory() as session:
            summary = await run_scheduler_tick(
                session,
                client,
                city_config=city_config,
                source_directory=args.sources,
                now=now,
                metrics_provider=UnavailablePostMetricsProvider(),
            )
    return {"ok": True, "now": now.isoformat(), **asdict(summary)}


def _status(args: argparse.Namespace) -> dict[str, Any]:
    limit = max(1, min(args.limit, 200))
    with SessionFactory() as session:
        runs = list(
            session.scalars(
                select(SchedulerRun)
                .where(SchedulerRun.city == args.city)
                .order_by(SchedulerRun.started_at.desc(), SchedulerRun.id.desc())
                .limit(limit)
            )
        )
    return {
        "ok": True,
        "city": args.city,
        "runs": [
            {
                "id": run.id,
                "task_key": run.task_key,
                "task_type": run.task_type,
                "scheduled_for": run.scheduled_for.isoformat(),
                "attempt": run.attempt,
                "status": run.status,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "error_type": run.error_type,
                "error_message": run.error_message,
                "details": run.details,
            }
            for run in runs
        ],
    }


def main() -> None:
    args = _parser().parse_args()
    if args.command == "tick":
        result = asyncio.run(_tick(args))
    elif args.command == "status":
        result = _status(args)
    else:
        raise RuntimeError(f"unknown command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
