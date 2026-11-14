"""Narrow deterministic exclusions from the Belgrade general-interest feed."""

import re
from urllib.parse import urlsplit

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


_OUTAGE_NOTICE = re.compile(
    r"\b(?:kvarov\w*|кваров\w*|bez\s+v\s*ode|без\s+вод\w*|"
    r"isključenj\w*|искључењ\w*|отключен\w*)\b", re.IGNORECASE,
)
_NOTICE_TIME = re.compile(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b")


def local_utility_notice(source_id: str, url: str, title: str, body: str) -> bool:
    """Recognize actionable outages from dedicated, registered city or utility sources."""
    if source_id == "city_beograd_news":
        try:
            path = urlsplit(url).path
        except ValueError:
            return False
        if not any(f"/servisne-informacije/{section}/" in path
                   for section in ("vodovod-i-kanalizacija", "elektricna-energija")):
            return False
    elif source_id != "bvk_outage_news" and not source_id.startswith("eds_belgrade_outages_"):
        return False
    return bool(_OUTAGE_NOTICE.search(f"{title}\n{body}") and _NOTICE_TIME.search(body))
