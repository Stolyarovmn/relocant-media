import json
import sys
from pathlib import Path

from relocant_media.ops.ad_staging_check import main, run_staging_check


def test_advertising_staging_check_is_offline_and_fail_closed() -> None:
    result = run_staging_check(
        pricing_policy_path=Path("cities/belgrade/ad-pricing.yaml"),
    )

    assert result["passed"] is True
    assert result["environment"] == "synthetic_staging"
    assert result["network_calls"] == 0
    assert result["telegram_mutations"] == 0
    assert result["ads_money_mutations"] == 0
    assert result["pricing_policy_configured"] is False
    assert result["pricing_status"] == "owner_configuration_required"
    assert all(result["assertions"].values())


def test_advertising_staging_check_cli_reports_safe_result(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-ad-staging-check",
            "--pricing-policy",
            "cities/belgrade/ad-pricing.yaml",
        ],
    )

    assert main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["passed"] is True
    assert output["network_calls"] == 0
    assert output["telegram_mutations"] == 0
    assert output["ads_money_mutations"] == 0
