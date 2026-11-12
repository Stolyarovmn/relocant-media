"""Узкие адаптеры опубликованных афиш, без сетевых запросов и геокодирования."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.city_profile import CityEditorialProfile
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.exhibition_map_point import MAX_HTML_BYTES

_DOB_HOSTS = {"domomladine.org", "www.domomladine.org"}
_DOB_CONTACT = "https://domomladine.org/kontakt/"
_DOB_OSM = "https://www.openstreetmap.org/way/41234985"
_DOB_EVENT_PATH = re.compile(r"/koncerti/[a-z0-9-]+/\Z")
_DOB_POST_PATH = re.compile(r"/[a-z0-9-]+/\Z")
_DOB_ROOMS = {
    "dob//amerikana": "Dom omladine Beograda · Amerikana",
    "dob//velika sala": "Dom omladine Beograda · Velika sala",
    "dob//klub": "Dom omladine Beograda · Klub DOB",
}
_MONTHS = (
    ("januar", "januara"), ("februar", "februara"), ("mart", "marta"),
    ("april", "aprila"), ("maj", "maja"), ("jun", "juna"), ("jul", "jula"),
    ("avgust", "avgusta"), ("septembar", "septembra"), ("oktobar", "oktobra"),
    ("novembar", "novembra"), ("decembar", "decembra"),
)
_MONTH = {name: number for number, names in enumerate(_MONTHS, 1) for name in names}
_WEEKDAYS = (
    "ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja",
)
_EXACT_DATE = re.compile(
    r"(?:(?P<weekday>[^,\d]+),\s*)?(?P<day>\d{1,2})\.\s+"
    r"(?P<month>[a-z]+)\s+(?P<year>20\d{2})\.\s+u\s+"
    r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})",
    re.IGNORECASE,
)
_ARTICLE_TIME = re.compile(
    r"\b(?P<day>\d{1,2})\.\s+(?P<month>[a-z]+)"
    r"(?:\s+(?P<year>20\d{2})\.)?\s+(?:od|u)\s+"
    r"(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2})|\s+časova)\b",
    re.IGNORECASE,
)
_DOB_MENTION = re.compile(r"\bdomu omladine beograda\b", re.IGNORECASE)
_CHANGE = re.compile(r"\b(?:otkazan\w*|odložen\w*|premešten\w*)\b", re.IGNORECASE)


def _tag(root: BeautifulSoup | Tag, selector: str) -> Tag | None:
    elements = root.select(selector, limit=2)
    return elements[0] if len(elements) == 1 else None


def _text(tag: Tag | None) -> str:
    return " ".join(tag.get_text(" ", strip=True).split()) if tag is not None else ""


def _dob_url(url: str) -> str:
    if not is_public_https_url(url):
        return ""
    parts = urlsplit(url)
    if parts.hostname not in _DOB_HOSTS or parts.query or parts.fragment or parts.port is not None:
        return ""
    return parts.path


def _start(value: str) -> datetime | None:
    match = _EXACT_DATE.fullmatch(value)
    if not match:
        return None
    try:
        date = datetime(
            int(match["year"]), _MONTH[match["month"].casefold()], int(match["day"]),
            int(match["hour"]), int(match["minute"]),
        )
    except (ValueError, KeyError):
        return None
    weekday = match["weekday"]
    return date if weekday is None or weekday.casefold() == _WEEKDAYS[date.weekday()] else None


def _lead_agrees(lead: str, start: datetime) -> bool:
    dates = list(_ARTICLE_TIME.finditer(lead))
    if len(dates) != 1 or not _DOB_MENTION.search(lead):
        return False
    match = dates[0]
    return (int(match["day"]) == start.day and _MONTH.get(match["month"].casefold()) == start.month
            and (match["year"] is None or int(match["year"]) == start.year)
            and int(match["hour"]) == start.hour and int(match["minute"] or 0) == start.minute)


def _linked_card(
    soup: BeautifulSoup, article: Tag, url: str, document: EvidenceDocument,
) -> tuple[str, str, datetime, str] | None:
    lead = _tag(article, "header.entry-header > .entry-excerpt")
    if lead is None:
        return None
    links = [
        (urljoin(url, href), anchor) for anchor in lead.select("a[href]")
        if isinstance(href := anchor.get("href"), str)
        and _DOB_EVENT_PATH.fullmatch(_dob_url(urljoin(url, href)))
    ]
    if len(links) != 1:
        return None
    card_url, anchor = links[0]
    name = _text(anchor).strip("„“”\" ")
    if not name or name.casefold() not in " ".join(document.body.split()).casefold():
        return None
    cards = [
        slide for slide in soup.select(".events-slider-wrapper .event-slide")
        if (link := _tag(slide, "h3.entry-title > a[href]")) is not None
        and isinstance(href := link.get("href"), str)
        and urljoin(url, href) == card_url
    ]
    if len(cards) != 1:
        return None
    card = cards[0]
    title = _text(_tag(card, "h3.entry-title > a > .main-title"))
    start = _start(_text(_tag(card, "p.slide-location")))
    if start is None or name.casefold() not in title.casefold():
        return None
    if _CHANGE.search(title) or not _lead_agrees(_text(lead), start):
        return None
    return name, _text(_tag(card, "p.slide-date")), start, card_url


def _dob_facts(document: EvidenceDocument, soup: BeautifulSoup) -> dict[str, Any] | None:
    path = _dob_url(document.url)
    direct = _DOB_EVENT_PATH.fullmatch(path)
    if direct:
        article = _tag(soup, "#content article.type-concert.status-publish")
    elif _DOB_POST_PATH.fullmatch(path):
        article = _tag(soup, "#content article.type-post.status-publish")
    else:
        return None
    if article is None:
        return None
    heading = _text(_tag(article, "h1.entry-title > .main-title"))
    lead = _text(_tag(article, "header.entry-header > .entry-excerpt"))
    if not heading or _CHANGE.search(heading + " " + lead):
        return None
    if direct:
        date = _text(_tag(article, "header.entry-header > p.entry-datetime > b"))
        start = _start(date)
        if start is None:
            return None
        room = _text(_tag(article, "header.entry-header > p.entry-datetime > i"))
        name, basis = heading, "source_dob_concert_header"
        schedule_url = document.url
    else:
        linked = _linked_card(soup, article, document.url, document)
        if linked is None:
            return None
        name, room, start, schedule_url = linked
        basis = "source_dob_linked_concert"
    venue = _DOB_ROOMS.get(room.casefold())
    if venue is None or len(name) > 260:
        return None
    # Адрес подтверждён контактами DOB, точка совпадает с домом 22 в индексе OSM.
    return {
        "event_name": name,
        "venue": {"name": venue, "address": "Makedonska 22/IV, Beograd",
                  "latitude": 44.815407, "longitude": 20.462793},
        "starts_at": start.isoformat(),
        "ends_at": None,
        "time_basis": basis,
        "schedule_source_url": schedule_url,
        "coordinate_source_url": _DOB_OSM,
        "venue_source_url": _DOB_CONTACT,
    }


def extract_event_adapter(
    document: EvidenceDocument, source_html: str, *, profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    """Выделяет только явно датированное событие с подтверждённой площадкой."""
    if (profile.city_id != "belgrade" or document.source_id != "venue_youth_centre"
            or not _dob_url(document.url) or not isinstance(source_html, str)
            or len(source_html.encode()) > MAX_HTML_BYTES):
        return None
    soup = BeautifulSoup(source_html, "html.parser")
    canonical = _tag(soup, 'head link[rel="canonical"][href]')
    if canonical is None or canonical.get("href") != document.url:
        return None
    return _dob_facts(document, soup)
