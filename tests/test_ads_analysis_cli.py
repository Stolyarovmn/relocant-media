import json
from decimal import Decimal

import pytest

from relocant_media.ads.analysis import AdsAnalysisPolicy, analyze_ads
from relocant_media.ops.ads_analysis_cli import load_source


def test_json_source_loads_export_without_credentials(tmp_path) -> None:
    source_file = tmp_path / "ads.json"
    source_file.write_text(
        json.dumps(
            {
                "campaigns": [
                    {
                        "campaign_id": "c1",
                        "name": "Belgrade launch",
                        "status": "active",
                        "currency": "EUR",
                        "captured_at": "2026-10-21T15:00:00Z",
                        "spend": "15.50",
                        "impressions": 2000,
                        "clicks": 100,
                        "joins": 8,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    source = load_source(source_file)
    report = analyze_ads(
        source,
        policy=AdsAnalysisPolicy(target_cost_per_join=Decimal("3")),
    )
    assert report["summary"]["spend"] == 15.5
    assert report["summary"]["joins"] == 8
    assert report["mutation_capability"] is False


def test_json_source_requires_timezone(tmp_path) -> None:
    source_file = tmp_path / "ads.json"
    source_file.write_text(
        json.dumps(
            {
                "campaigns": [
                    {
                        "campaign_id": "c1",
                        "name": "Belgrade launch",
                        "status": "active",
                        "currency": "EUR",
                        "captured_at": "2026-10-21T15:00:00",
                        "spend": "15.50",
                        "impressions": 2000,
                        "clicks": 100,
                        "joins": 8,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="timezone"):
        load_source(source_file)
