from relocant_media.domain.models import RiskClass
from relocant_media.domain.policy import requires_human_review, risk_for_category


def test_high_risk_is_never_auto_in_v1() -> None:
    assert risk_for_category("immigration") is RiskClass.HIGH
    assert requires_human_review("immigration", 1.0) is True


def test_low_risk_threshold() -> None:
    assert requires_human_review("transport", 0.92) is True
    assert requires_human_review("transport", 0.93) is False
