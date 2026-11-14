"""Narrow deterministic exclusions from the Belgrade general-interest feed."""

import re

from relocant_media.domain.attention import normalize

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


_FLIGHT = re.compile(
    r"\b(?:letov\w*|letove|leta|летов\w*|летове|лета|авиарейс\w*|рейс\w*|flights?)\b"
)
_FLIGHT_CHANGE = re.compile(
    r"\b(?:otkaz\w*|отказ\w*|obustav\w*|обустав\w*|ukida\w*|укида\w*|"
    r"povuk\w*|повук\w*|povlač\w*|повлач\w*|отмен\w*|приостан\w*|"
    r"возобнов\w*|cancel\w*|suspend\w*|withdraw\w*|resume\w*)\b"
)


def local_flight_change(title: str, body: str, city_aliases: tuple[str, ...]) -> bool:
    """Flag a passenger service change for a second look at editorial relevance."""
    headline = normalize(title)
    if not (_FLIGHT.search(headline) and _FLIGHT_CHANGE.search(headline)):
        return False
    lead = normalize(f"{title}\n{body[:1200]}")
    return any(
        re.search(r"(?<!\w)" + re.escape(alias) + r"(?:a|u|а|е|ом)?(?!\w)", lead)
        for name in city_aliases if (alias := normalize(name))
    )
