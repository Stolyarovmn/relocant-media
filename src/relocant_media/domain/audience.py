"""Narrow deterministic exclusions from the Belgrade general-interest feed."""

import re
from urllib.parse import urlsplit

from relocant_media.domain.attention import normalize
from relocant_media.domain.freshness import has_service_day

AUDIENCE_POLICY_VERSION = "belgrade-audience-v1"
READER_VALUE_POLICY_VERSION = "reader-value-v2"

_ROAD = re.compile(
    r"\b(?:ulic\w*|deonic\w*|raskrsnic\w*|bulevar\w*|tunel\w*|"
    r"улиц\w*|деониц\w*|раскрсниц\w*|булевар\w*|тунел\w*|"
    r"дорог\w*|перекр[её]ст\w*|бульвар\w*|тоннел\w*)\b",
    re.IGNORECASE,
)
_ROAD_OBJECT = (
    r"(?:ulic[aeu]|deonic[aeu]|raskrsnic[aeu]|bulevar|tunel|"
    r"улиц[ауы]|део\s+улице|деониц[аеу]|раскрсниц[аеу]|булевар|тунел|дорог[ауы]|"
    r"перекр[её]сток|бульвар|тоннель)"
)
_CLOSED = (
    r"(?:zatvoren[aeiou]?|zatvoril[aoei]|zatvori[ćc]e\s+se|zatvara\s+se|obustavljen[ao]?|"
    r"затворен[аеиоу]?|затворил[аоеи]|затвориће\s+се|затвара\s+се|обустављен[ао]?|"
    r"перекрыт[аоы]?|перекрыли|перекрывают|перекроют|"
    r"закрыт[аоы]?|закрыли|закрывают|закроют)"
)
_ROAD_CLOSED = re.compile(
    rf"\b{_ROAD_OBJECT}(?:\s+(?:je|su|је|су|будет|будут|частично|полностью)){{0,2}}\s+{_CLOSED}\b"
    rf"|\b{_CLOSED}(?:\s+(?:je|su|је|су|deo|део|часть|участок)){{0,2}}\s+{_ROAD_OBJECT}\b"
    rf"|\b{_CLOSED}\s+(?:za\s+saobra[ćc]aj|за\s+саобраћај|для\s+(?:проезда|движения))\b",
    re.IGNORECASE,
)
_UNCERTAIN_CLOSURE = re.compile(
    r"\b(?:mogu[ćc]\w*|могућ\w*|mo[žz]da|можда|mogl[aoei]|могл[аоеи]|"
    r"ako|ако|если|возможно|могут|может|обсуждают|"
    r"(?:nije|nisu|није|нису|не)\s+(?:jo[šs]\s+|још\s+|ещ[её]\s+)?"
    r"(?:zatvor\w*|затвор\w*|перекры\w*|перекро\w*|закры\w*|закро\w*))\b",
    re.IGNORECASE,
)


def local_road_closure(title: str, body: str, locality: re.Pattern[str]) -> bool:
    """Признак для порядка разбора. Локальность, дорога и статус нужны в одной фразе."""
    phrases = re.split(r"[\n;]+|(?<=[.!?])\s+", f"{title}\n{body[:500]}")
    return any(
        locality.search(phrase) and _ROAD.search(phrase) and _ROAD_CLOSED.search(phrase)
        and not _UNCERTAIN_CLOSURE.search(phrase)
        for phrase in phrases
    )


def local_reader_interest(title: str, body: str, city_aliases: tuple[str, ...]) -> bool:
    """Повод для повторного рассмотрения, не автоматическое одобрение новости."""
    text = normalize(title + "\n" + body[:1200])
    local = any(re.search(r"(?<!\w)" + re.escape(normalize(alias)) + r"\w*", text)
                for alias in city_aliases)
    return local and re.search(
        r"koncert|festival|izlož|muzej|kultur|pozori|istorij|истори|театр|культур|выстав|музе|"
        r"sport|sports|vaterpol|košark|fudbal|liga|utakmic|klub|"
        r"спорт|ватерпол|кошарк|фудбал|клуб|матч|лиг|жител|stanovnik|komšij|inicijativ|"
        r"фестивал|концерт|позориш|становник|комшиј|иницијатив",
        text, re.IGNORECASE,
    ) is not None


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
    return bool(
        _OUTAGE_NOTICE.search(f"{title}\n{body}")
        and (_NOTICE_TIME.search(body) or has_service_day(title, body))
    )


def official_water_notice(source_id: str, url: str, title: str, body: str) -> bool:
    """Recognize only dated, registered water notices for the official water route."""
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.username is not None or parts.password is not None:
            return False
    except ValueError:
        return False
    is_city_water = (
        source_id == "city_beograd_news" and parts.hostname in {"beograd.rs", "www.beograd.rs"}
        and "/servisne-informacije/vodovod-i-kanalizacija/" in parts.path
    )
    is_water_utility = source_id == "bvk_outage_news" and parts.hostname in {"bvk.rs", "www.bvk.rs"}
    return (
        (is_city_water or is_water_utility) and has_service_day(title, body)
        and local_utility_notice(source_id, url, title, body)
    )
