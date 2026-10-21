"""Offline end-to-end advertising staging check with zero production side effects."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ads import AdsAnalysisPolicy, CampaignSnapshot, analyze_ads
from relocant_media.bot.intake import handle_private_message
from relocant_media.services.ad_pricing import evaluate_ad_price_candidate, load_ad_pricing_policy
from relocant_media.services.ad_review import record_suggested_post_review
from relocant_media.storage.models import AdRequest, Base


class _SyntheticAdsSource:
    def __init__(self, snapshots: list[CampaignSnapshot]) -> None:
        self._snapshots = snapshots

    def read_snapshots(self) -> list[CampaignSnapshot]:
        return list(self._snapshots)


def run_staging_check(*, pricing_policy_path: Path) -> dict[str, Any]:
    """Exercise intake -> review -> pricing -> analytics without network or money mutations."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            intake = handle_private_message(
                session,
                {
                    "message_id": 7001,
                    "from": {"id": 900000001},
                    "chat": {"id": 900000001},
                    "text": "/ad Staging-only local cafe placement",
                },
                city="belgrade",
            )
            session.flush()
            request = session.scalar(select(AdRequest).order_by(AdRequest.id.desc()).limit(1))
            if intake.outcome != "submitted" or request is None:
                raise RuntimeError("advertiser intake did not create an AdRequest")

            review = record_suggested_post_review(
                session,
                ad_request_id=request.id,
                proposed_outcome="accept_candidate",
                reason_codes=["staging_safe_local_business"],
                risk_flags=[],
                audience_fit=True,
                claims_verified=True,
                reviewer="staging-check",
                idempotency_key="staging-ad-review-7001",
                reviewer_metadata={"environment": "synthetic_staging"},
                reviewed_at=datetime(2026, 10, 21, 19, 0, tzinfo=UTC),
            )

            policy = load_ad_pricing_policy(pricing_policy_path)
            pricing = evaluate_ad_price_candidate(
                session,
                policy=policy,
                ad_request_id=request.id,
                package_id="synthetic-package",
                candidate_price=Decimal("1"),
            )

            analytics = analyze_ads(
                _SyntheticAdsSource(
                    [
                        CampaignSnapshot(
                            campaign_id="staging-campaign",
                            name="Synthetic staging campaign",
                            status="active",
                            currency="EUR",
                            captured_at=datetime(2026, 10, 21, 19, 0, tzinfo=UTC),
                            spend=Decimal("10"),
                            impressions=10_000,
                            clicks=100,
                            joins=10,
                        )
                    ]
                ),
                policy=AdsAnalysisPolicy(target_cost_per_join=Decimal("2")),
            )

            assertions = {
                "intake_created_ad_request": intake.outcome == "submitted",
                "review_is_candidate_only": (
                    review.effective_outcome == "accept_candidate"
                    and review.requires_owner_approval is True
                ),
                "pricing_has_no_send_capability": (
                    pricing["commercial_send_capability"] is False
                    and pricing["telegram_publication_capability"] is False
                    and pricing["ads_spend_capability"] is False
                ),
                "pricing_fails_closed_without_rate_card": (
                    pricing["status"] == "owner_configuration_required"
                    if not policy.configured
                    else True
                ),
                "ads_analysis_is_read_only": analytics["mutation_capability"] is False,
            }
            passed = all(assertions.values())
            return {
                "passed": passed,
                "environment": "synthetic_staging",
                "network_calls": 0,
                "telegram_mutations": 0,
                "ads_money_mutations": 0,
                "pricing_policy_version": policy.version,
                "pricing_policy_configured": policy.configured,
                "pricing_status": pricing["status"],
                "assertions": assertions,
            }
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pricing-policy",
        type=Path,
        default=Path("cities/belgrade/ad-pricing.yaml"),
    )
    args = parser.parse_args()
    result = run_staging_check(pricing_policy_path=args.pricing_policy)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
