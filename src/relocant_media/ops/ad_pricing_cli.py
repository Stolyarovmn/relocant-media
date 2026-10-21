"""Evaluate an owner-configured advertising price candidate without sending an offer."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from relocant_media.services.ad_pricing import (
    evaluate_ad_price_candidate,
    load_ad_pricing_policy,
)
from relocant_media.storage.db import SessionFactory


def _decimal(value: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("candidate price must be a decimal") from exc
    if not result.is_finite() or result <= 0:
        raise argparse.ArgumentTypeError("candidate price must be positive")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--ad-request-id", type=int, required=True)
    parser.add_argument("--package-id", required=True)
    parser.add_argument("--candidate-price", type=_decimal)
    args = parser.parse_args()

    try:
        policy = load_ad_pricing_policy(args.policy)
        with SessionFactory() as session:
            result = evaluate_ad_price_candidate(
                session,
                policy=policy,
                ad_request_id=args.ad_request_id,
                package_id=args.package_id,
                candidate_price=args.candidate_price,
            )
    except Exception as exc:
        parser.exit(1, f"Pricing candidate evaluation failed: {exc}\n")

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
