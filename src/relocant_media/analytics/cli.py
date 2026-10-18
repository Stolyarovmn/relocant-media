import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from relocant_media.analytics.measurements import (
    record_channel_snapshot,
    upsert_acquisition_cohort,
)
from relocant_media.analytics.report import build_daily_report, store_daily_report
from relocant_media.storage.db import SessionFactory


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Relocant Media measurements")
    subparsers = parser.add_subparsers(dest="command", required=True)

    channel = subparsers.add_parser("channel", help="Record a channel subscriber snapshot")
    channel.add_argument("--city", required=True)
    channel.add_argument("--subscribers", required=True, type=int)
    channel.add_argument("--captured-at", required=True, type=_aware_datetime)
    channel.add_argument("--source", default="manual")
    channel.add_argument("--kind", choices=("observed", "correction"), default="observed")
    channel.add_argument("--note")

    acquisition = subparsers.add_parser("acquisition", help="Upsert an acquisition cohort")
    acquisition.add_argument("--city", required=True)
    acquisition.add_argument("--campaign", required=True)
    acquisition.add_argument("--date", required=True, type=date.fromisoformat)
    acquisition.add_argument("--joins", required=True, type=int)
    acquisition.add_argument("--spend", type=Decimal)
    acquisition.add_argument("--currency")
    acquisition.add_argument("--d1", type=int)
    acquisition.add_argument("--d7", type=int)
    acquisition.add_argument("--d30", type=int)
    acquisition.add_argument("--source", default="manual")

    report = subparsers.add_parser("report", help="Build and store a daily report")
    report.add_argument("--city", required=True)
    report.add_argument("--date", required=True, type=date.fromisoformat)
    report.add_argument("--timezone", required=True)
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    with SessionFactory() as session:
        if args.command == "channel":
            record_channel_snapshot(
                session,
                city=args.city,
                captured_at=args.captured_at,
                subscribers=args.subscribers,
                source=args.source,
                kind=args.kind,
                note=args.note,
            )
            session.commit()
            return {"ok": True, "measurement": "channel"}

        if args.command == "acquisition":
            upsert_acquisition_cohort(
                session,
                city=args.city,
                campaign=args.campaign,
                cohort_date=args.date,
                joins=args.joins,
                spend=args.spend,
                currency=args.currency,
                retained_d1=args.d1,
                retained_d7=args.d7,
                retained_d30=args.d30,
                source=args.source,
            )
            session.commit()
            return {"ok": True, "measurement": "acquisition"}

        if args.command == "report":
            payload = build_daily_report(
                session,
                report_date=args.date,
                city=args.city,
                timezone_name=args.timezone,
            )
            store_daily_report(session, report_date=args.date, city=args.city, payload=payload)
            session.commit()
            return payload

        raise RuntimeError(f"unknown command: {args.command}")


def main() -> None:
    args = _parser().parse_args()
    print(json.dumps(_run(args), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
