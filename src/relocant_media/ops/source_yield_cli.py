"""Print a read-only source-yield funnel for a bounded ingestion window."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from relocant_media.analytics.source_yield import build_source_yield_report
from relocant_media.storage.db import SessionFactory


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="belgrade")
    parser.add_argument("--since", required=True, type=_aware_datetime)
    parser.add_argument("--until", required=True, type=_aware_datetime)
    args = parser.parse_args()

    with SessionFactory() as session:
        report = build_source_yield_report(
            session,
            city=args.city,
            since=args.since,
            until=args.until,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
