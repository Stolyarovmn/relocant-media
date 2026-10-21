"""Narrow deterministic exclusions from the Belgrade general-interest feed."""

import re

AUDIENCE_POLICY_VERSION = "belgrade-audience-v1"
_PROCUREMENT = re.compile(
    r"\b(?:јавн\w*\s+набав\w*|javn\w*\s+nabav\w*|"
    r"гос(?:ударственн\w*)?\s*закуп\w*|public\s+procurement)\b", re.IGNORECASE)
_TRAINING = re.compile(
    r"\b(?:радиониц\w*|radionic\w*|мастерск\w*|семинар\w*|seminar\w*|"
    r"обук\w*|obuk\w*|workshop\w*|training)\b", re.IGNORECASE)


def audience_relevance_reason(title: str, body: str) -> str | None:
    """Exclude procurement training, not a whole venue or all professional news."""
    text = f"{title}\n{body}"
    if _PROCUREMENT.search(text) and _TRAINING.search(text):
        return "audience_niche_procurement_training"
    return None
