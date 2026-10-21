from datetime import UTC, datetime
from decimal import Decimal

import pytest

from relocant_media.ads.analysis import (
    AdsAnalysisPolicy,
    AdsReadOnlySource,
    CampaignSnapshot,
    analyze_ads,
)


class FakeReadOnlySource:
    def __init__(self, snapshots: list[CampaignSnapshot]) -> None:
        self._snapshots = snapshots

    def read_snapshots(self) -> list[CampaignSnapshot]:
        return list(self._snapshots)


def _snapshot(
    campaign_id: str,
    *,
    spend: str = "20",
    impressions: int = 2_000,
    clicks: int = 100,
    joins: int = 10,
    status: str = "active",
    currency: str = "EUR",
) -> CampaignSnapshot:
    return CampaignSnapshot(
        campaign_id=campaign_id,
        name=f"Campaign {campaign_id}",
        status=status,
        currency=currency,
        captured_at=datetime(2026, 10, 21, 15, 0, tzinfo=UTC),
        spend=Decimal(spend),
        impressions=impressions,
        clicks=clicks,
        joins=joins,
    )


def _policy() -> AdsAnalysisPolicy:
    return AdsAnalysisPolicy(target_cost_per_join=Decimal("3"))


def test_read_only_contract_exposes_no_mutation_methods() -> None:
    public = {
        name
        for name, value in AdsReadOnlySource.__dict__.items()
        if not name.startswith("_") and callable(value)
    }
    assert public == {"read_snapshots"}


def test_analysis_proposes_scale_or_investigation_without_mutation_capability() -> None:
    source = FakeReadOnlySource(
        [
            _snapshot("efficient", spend="20", joins=10),
            _snapshot("expensive", spend="60", joins=10),
        ]
    )
    report = analyze_ads(source, policy=_policy())

    assert report["trust_domain"] == "ads_read_only"
    assert report["mutation_capability"] is False
    campaigns = {row["campaign_id"]: row for row in report["campaigns"]}
    assert campaigns["efficient"]["recommendation"] == "consider_scale"
    assert campaigns["expensive"]["recommendation"] == "investigate"
    for campaign in campaigns.values():
        assert campaign["proposed_only"] is True
        assert campaign["requires_owner_approval_for_mutation"] is True


def test_small_sample_is_observation_not_scale_recommendation() -> None:
    report = analyze_ads(
        FakeReadOnlySource([_snapshot("small", impressions=500, clicks=20, joins=5)]),
        policy=_policy(),
    )
    campaign = report["campaigns"][0]
    assert campaign["recommendation"] == "observe"
    assert campaign["reason_code"] == "insufficient_impressions"


def test_invalid_counter_relationships_are_rejected() -> None:
    with pytest.raises(ValueError, match="clicks cannot exceed impressions"):
        analyze_ads(
            FakeReadOnlySource([_snapshot("bad", impressions=10, clicks=11, joins=1)]),
            policy=_policy(),
        )
    with pytest.raises(ValueError, match="joins cannot exceed clicks"):
        analyze_ads(
            FakeReadOnlySource([_snapshot("bad", impressions=100, clicks=10, joins=11)]),
            policy=_policy(),
        )


def test_multiple_currencies_are_not_aggregated() -> None:
    with pytest.raises(ValueError, match="multiple currencies"):
        analyze_ads(
            FakeReadOnlySource(
                [_snapshot("eur"), _snapshot("usd", currency="USD")]
            ),
            policy=_policy(),
        )


def test_empty_source_has_no_mutation_capability() -> None:
    report = analyze_ads(FakeReadOnlySource([]), policy=_policy())
    assert report["campaigns"] == []
    assert report["mutation_capability"] is False
    assert report["summary"] == {
        "campaigns": 0,
        "spend": 0.0,
        "impressions": 0,
        "joins": 0,
    }
