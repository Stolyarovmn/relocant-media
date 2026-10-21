"""Record an audited reader-correction decision without editing Telegram."""

from __future__ import annotations

import argparse
import json

from relocant_media.services.correction_control import (
    ALLOWED_CORRECTION_DECISIONS,
    record_reader_correction_decision,
)
from relocant_media.storage.db import SessionFactory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-id", type=int, required=True)
    parser.add_argument("--decision", choices=sorted(ALLOWED_CORRECTION_DECISIONS), required=True)
    parser.add_argument("--reason-code", required=True)
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--note")
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="required acknowledgement that an append-only correction decision will be recorded",
    )
    args = parser.parse_args()
    if not args.confirm_write:
        parser.exit(2, "--confirm-write is required; no database change was made.\n")

    try:
        with SessionFactory() as session:
            row = record_reader_correction_decision(
                session,
                submission_id=args.submission_id,
                decision=args.decision,
                reason_code=args.reason_code,
                actor=args.actor,
                idempotency_key=args.idempotency_key,
                note=args.note,
            )
            session.commit()
            payload = {
                "decision_id": row.id,
                "submission_id": row.submission_id,
                "decision": row.decision,
                "reason_code": row.reason_code,
                "actor": row.actor,
                "telegram_edit_performed": False,
                "automatic_publication": False,
            }
    except Exception as exc:
        parser.exit(1, f"Reader correction decision was not recorded: {exc}\n")

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
