"""Print read-only category learning signals for a bounded publication window."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from relocant_media.analytics.category_learning import build_category_learning_report
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
    parser.add_argument("--horizon-hours", type=int, default=24)
    parser.add_argument("--minimum-samples", type=int, default=5)
    args = parser.parse_args()

    with SessionFactory() as session:
        report = build_category_learning_report(
            session,
            city=args.city,
            since=args.since,
            until=args.until,
            horizon_hours=args.horizon_hours,
            minimum_samples=args.minimum_samples,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
