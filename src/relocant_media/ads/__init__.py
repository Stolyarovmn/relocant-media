"""Read-only advertising analytics trust domain."""

from relocant_media.ads.analysis import (
    AdsAnalysisPolicy,
    AdsReadOnlySource,
    CampaignSnapshot,
    analyze_ads,
)

__all__ = [
    "AdsAnalysisPolicy",
    "AdsReadOnlySource",
    "CampaignSnapshot",
    "analyze_ads",
]
