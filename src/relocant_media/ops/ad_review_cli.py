"""Record a candidate-only suggested-post review; never publish or spend money."""

from __future__ import annotations

import argparse
import json

from relocant_media.services.ad_review import (
    ALLOWED_AD_REVIEW_OUTCOMES,
    record_suggested_post_review,
)
from relocant_media.storage.db import SessionFactory


def _bool_choice(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"yes", "true", "1"}:
        return True
    if normalized in {"no", "false", "0"}:
        return False
    raise argparse.ArgumentTypeError("expected yes/no")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ad-request-id", type=int, required=True)
    parser.add_argument(
        "--proposed-outcome",
        choices=sorted(ALLOWED_AD_REVIEW_OUTCOMES),
        required=True,
    )
    parser.add_argument("--reason-code", action="append", required=True)
    parser.add_argument("--risk-flag", action="append", default=[])
    parser.add_argument("--audience-fit", type=_bool_choice, required=True)
    parser.add_argument("--claims-verified", type=_bool_choice, required=True)
    parser.add_argument("--reviewer", default="operator")
    parser.add_argument("--reviewer-metadata-json", default="{}")
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument(
        "--confirm-write",
        action="store_true",
        help="required acknowledgement that an append-only candidate review will be recorded",
    )
    args = parser.parse_args()
    if not args.confirm_write:
        parser.exit(2, "--confirm-write is required; no database change was made.\n")

    try:
        metadata = json.loads(args.reviewer_metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("reviewer metadata must be a JSON object")
        with SessionFactory() as session:
            row = record_suggested_post_review(
                session,
                ad_request_id=args.ad_request_id,
                proposed_outcome=args.proposed_outcome,
                reason_codes=args.reason_code,
                risk_flags=args.risk_flag,
                audience_fit=args.audience_fit,
                claims_verified=args.claims_verified,
                reviewer=args.reviewer,
                idempotency_key=args.idempotency_key,
                reviewer_metadata=metadata,
            )
            session.commit()
            payload = {
                "review_id": row.id,
                "ad_request_id": row.ad_request_id,
                "proposed_outcome": row.proposed_outcome,
                "effective_outcome": row.effective_outcome,
                "reason_codes": row.reason_codes,
                "requires_owner_approval": row.requires_owner_approval,
                "telegram_publication_performed": False,
                "ads_spend_performed": False,
            }
    except Exception as exc:
        parser.exit(1, f"Suggested-post review was not recorded: {exc}\n")

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
