"""Deterministic read-only analysis of externally supplied Telegram Ads snapshots.

The contract intentionally has no campaign mutation surface. It can consume snapshots and
produce proposals, but it cannot create campaigns, change bids/budgets, or spend money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CampaignSnapshot:
    campaign_id: str
    name: str
    status: str
    currency: str
    captured_at: datetime
    spend: Decimal
    impressions: int
    clicks: int
    joins: int


@dataclass(frozen=True, slots=True)
class AdsAnalysisPolicy:
    target_cost_per_join: Decimal
    minimum_impressions: int = 1_000
    minimum_joins_for_efficiency_decision: int = 3
    high_cost_multiplier: Decimal = Decimal("1.5")


class AdsReadOnlySource(Protocol):
    def read_snapshots(self) -> list[CampaignSnapshot]: ...


def _validate_snapshot(snapshot: CampaignSnapshot) -> None:
    if not snapshot.campaign_id.strip():
        raise ValueError("campaign_id is required")
    if not snapshot.currency.strip():
        raise ValueError("currency is required")
    if snapshot.spend < 0:
        raise ValueError("spend cannot be negative")
    if min(snapshot.impressions, snapshot.clicks, snapshot.joins) < 0:
        raise ValueError("campaign counters cannot be negative")
    if snapshot.clicks > snapshot.impressions:
        raise ValueError("clicks cannot exceed impressions")
    if snapshot.joins > snapshot.clicks:
        raise ValueError("joins cannot exceed clicks")


def _validate_policy(policy: AdsAnalysisPolicy) -> None:
    if policy.target_cost_per_join <= 0:
        raise ValueError("target_cost_per_join must be positive")
    if policy.minimum_impressions < 1:
        raise ValueError("minimum_impressions must be positive")
    if policy.minimum_joins_for_efficiency_decision < 1:
        raise ValueError("minimum_joins_for_efficiency_decision must be positive")
    if policy.high_cost_multiplier <= 1:
        raise ValueError("high_cost_multiplier must be greater than 1")


def _metrics(snapshot: CampaignSnapshot) -> dict[str, float | int | None]:
    impressions = snapshot.impressions
    clicks = snapshot.clicks
    joins = snapshot.joins
    spend = float(snapshot.spend)
    return {
        "impressions": impressions,
        "clicks": clicks,
        "joins": joins,
        "spend": spend,
        "ctr": clicks / impressions if impressions else None,
        "join_rate_from_click": joins / clicks if clicks else None,
        "cpm": spend * 1000 / impressions if impressions else None,
        "cost_per_click": spend / clicks if clicks else None,
        "cost_per_join": spend / joins if joins else None,
    }


def _recommendation(
    snapshot: CampaignSnapshot,
    policy: AdsAnalysisPolicy,
    metrics: dict[str, float | int | None],
) -> tuple[str, str]:
    if snapshot.status.lower() not in {"active", "running"}:
        return "observe", "campaign_not_active"
    if snapshot.impressions == 0:
        return "investigate", "no_delivery"
    if snapshot.impressions < policy.minimum_impressions:
        return "observe", "insufficient_impressions"
    if snapshot.joins < policy.minimum_joins_for_efficiency_decision:
        return "observe", "insufficient_join_sample"

    cost_per_join = metrics["cost_per_join"]
    assert isinstance(cost_per_join, float)
    target = float(policy.target_cost_per_join)
    if cost_per_join <= target:
        return "consider_scale", "cost_per_join_at_or_below_target"
    if cost_per_join >= target * float(policy.high_cost_multiplier):
        return "investigate", "cost_per_join_materially_above_target"
    return "maintain", "cost_per_join_near_target"


def analyze_ads(
    source: AdsReadOnlySource,
    *,
    policy: AdsAnalysisPolicy,
) -> dict[str, object]:
    """Analyze snapshots without exposing or invoking any mutation capability."""
    _validate_policy(policy)
    snapshots = source.read_snapshots()
    if not snapshots:
        return {
            "trust_domain": "ads_read_only",
            "mutation_capability": False,
            "campaigns": [],
            "summary": {"campaigns": 0, "spend": 0.0, "impressions": 0, "joins": 0},
            "limitations": ["No campaign snapshots were supplied."],
        }

    currencies = {snapshot.currency.upper() for snapshot in snapshots}
    if len(currencies) != 1:
        raise ValueError("one analysis report cannot aggregate multiple currencies")

    campaigns: list[dict[str, object]] = []
    total_spend = Decimal("0")
    total_impressions = 0
    total_clicks = 0
    total_joins = 0

    seen_ids: set[str] = set()
    for snapshot in snapshots:
        _validate_snapshot(snapshot)
        if snapshot.campaign_id in seen_ids:
            raise ValueError("duplicate campaign_id in snapshot batch")
        seen_ids.add(snapshot.campaign_id)
        metrics = _metrics(snapshot)
        recommendation, reason = _recommendation(snapshot, policy, metrics)
        campaigns.append(
            {
                "campaign_id": snapshot.campaign_id,
                "name": snapshot.name,
                "status": snapshot.status,
                "captured_at": snapshot.captured_at.isoformat(),
                "currency": snapshot.currency.upper(),
                "metrics": metrics,
                "recommendation": recommendation,
                "reason_code": reason,
                "requires_owner_approval_for_mutation": True,
                "proposed_only": True,
            }
        )
        total_spend += snapshot.spend
        total_impressions += snapshot.impressions
        total_clicks += snapshot.clicks
        total_joins += snapshot.joins

    aggregate_cpj = float(total_spend / total_joins) if total_joins else None
    campaigns.sort(key=lambda item: str(item["campaign_id"]))
    return {
        "trust_domain": "ads_read_only",
        "mutation_capability": False,
        "currency": next(iter(currencies)),
        "policy": {
            "target_cost_per_join": float(policy.target_cost_per_join),
            "minimum_impressions": policy.minimum_impressions,
            "minimum_joins_for_efficiency_decision": (
                policy.minimum_joins_for_efficiency_decision
            ),
            "high_cost_multiplier": float(policy.high_cost_multiplier),
        },
        "campaigns": campaigns,
        "summary": {
            "campaigns": len(campaigns),
            "spend": float(total_spend),
            "impressions": total_impressions,
            "clicks": total_clicks,
            "joins": total_joins,
            "cost_per_join": aggregate_cpj,
        },
        "limitations": [
            "Recommendations are proposals only and cannot mutate Telegram Ads.",
            (
                "Bid, budget, spend, campaign creation and pause/resume require "
                "a separate future executor."
            ),
            (
                "Any future money-changing action remains OWNER_APPROVAL until "
                "an explicit bounded policy exists."
            ),
            "Observed campaign performance is descriptive and does not prove causal lift.",
        ],
    }
