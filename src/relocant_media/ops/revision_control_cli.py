"""Record an explicit source-revision control decision; never publish or approve a story."""

from __future__ import annotations

import argparse
import json

from relocant_media.services.source_revision_control import (
    ALLOWED_REVISION_DECISIONS,
    record_source_revision_decision,
)
from relocant_media.storage.db import SessionFactory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision-id", type=int, required=True)
    parser.add_argument("--decision", choices=sorted(ALLOWED_REVISION_DECISIONS), required=True)
    parser.add_argument("--reason-code", required=True)
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument(
        "--expected-decision-count",
        type=int,
        required=True,
        help="decision_count from the control snapshot this write is based on",
    )
    parser.add_argument("--note")
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="required acknowledgement that an append-only control decision will be recorded",
    )
    args = parser.parse_args()
    if not args.confirm_write:
        parser.exit(2, "--confirm-write is required; no database change was made.\n")

    try:
        with SessionFactory() as session:
            row = record_source_revision_decision(
                session,
                revision_id=args.revision_id,
                decision=args.decision,
                reason_code=args.reason_code,
                actor=args.actor,
                idempotency_key=args.idempotency_key,
                expected_decision_count=args.expected_decision_count,
                note=args.note,
            )
            session.commit()
            payload = {
                "decision_id": row.id,
                "revision_id": row.revision_id,
                "decision": row.decision,
                "reason_code": row.reason_code,
                "actor": row.actor,
                "fresh_review_cleared": False,
                "telegram_side_effect": False,
            }
    except Exception as exc:
        parser.exit(1, f"Source revision decision was not recorded: {exc}\n")

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
