from collections.abc import Iterable

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


# Holds the editorial itself could not decide. The owner approves topic and substance;
# provenance, freshness and fact checks still run again before any delivery. Date and
# changed-source holds are provenance questions and are intentionally absent.
OWNER_DECISION_REASONS = frozenset({
    "borderline_relevance",
    "category_not_auto_publishable",
    "package_requires_review",
    "source_auto_publish_disabled",
    "policy_requires_human_review",
})


# A ranker "no" on a clearly relevant, confident item is a doubt, not a verdict: the item still
# gets claims, independent verification and a draft, and the owner decides. Clearly irrelevant,
# unsure and high-risk items stay skipped.
BORDERLINE_RELEVANCE = 0.6
BORDERLINE_CONFIDENCE = 0.6


def is_borderline(relevance: float, confidence: float, category: str) -> bool:
    return (
        relevance >= BORDERLINE_RELEVANCE
        and confidence >= BORDERLINE_CONFIDENCE
        and category.strip().lower() not in HIGH_RISK_CATEGORIES
    )


def primary_evidence_kind(ownerships: Iterable[str], category: str) -> str | None:
    """Official evidence is always enough; a verified media outlet only outside high-risk."""
    kinds = {value.strip().casefold() for value in ownerships}
    if "official" in kinds:
        return "official"
    if "media" in kinds and category.strip().lower() not in HIGH_RISK_CATEGORIES:
        return "media"
    return None


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
