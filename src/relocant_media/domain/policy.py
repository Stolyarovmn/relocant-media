from .models import RiskClass

CURRENT_EDITORIAL_RULE_VERSION = "belgrade-editorial-v2"

HIGH_RISK_CATEGORIES = {
    "immigration",
    "law",
    "tax",
    "medicine",
    "banking",
    "politics",
}


def risk_for_category(category: str) -> RiskClass:
    normalized = category.strip().lower()
    if normalized in HIGH_RISK_CATEGORIES:
        return RiskClass.HIGH
    if normalized in {"real_estate", "jobs", "schools", "utilities"}:
        return RiskClass.MEDIUM
    return RiskClass.LOW


def requires_human_review(category: str, confidence: float) -> bool:
    risk = risk_for_category(category)
    if risk is RiskClass.HIGH:
        return True
    if risk is RiskClass.MEDIUM:
        return confidence < 0.97
    return confidence < 0.93
