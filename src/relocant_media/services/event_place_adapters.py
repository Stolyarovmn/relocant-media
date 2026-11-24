"""Узкие адаптеры опубликованных афиш, без сетевых запросов и геокодирования."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.base import RawItem as Collected
from relocant_media.collectors.kolarac_program import saved_linked_program
from relocant_media.collectors.linked_tour_map import SOURCE_ID as TOUR_MAP_SOURCE
from relocant_media.collectors.linked_tour_map import parse_tour_place
from relocant_media.collectors.ticket_vision_events import SOURCE_ID as TICKET_SOURCE
from relocant_media.collectors.ticket_vision_events import parse_ticket_event
from relocant_media.domain.city_profile import DEFAULT_CITIES_ROOT, CityEditorialProfile
from relocant_media.domain.event_facts import event_dates
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.event_program import festival_program, registered_event_venues
from relocant_media.services.exhibition_map_point import MAX_HTML_BYTES
from relocant_media.services.primary_discovery import folded
from relocant_media.services.venue_event_evidence import matches_venue_evidence
from relocant_media.sources.registry import load_source_file

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
CUKARICA_STAGE = re.compile(
    r'на великој сцени културног центра\s*[„"«]?чукарица[“"»]?', re.IGNORECASE,
)
CUKARICA_GALLERY = re.compile(
    r'у галерији\s*[„"«]?старт\s*[’\x27]?06[“"»]?\s+културног центра\s*'
    r'[„"«]?чукарица[“"»]?', re.IGNORECASE,
)
_CUKARICA_DATE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\.\s+(?P<month>[а-яјљњћђџ]+)"
    r"(?:\s+(?P<year>20\d{2})\.?(?:\s+године)?)?,?\s+"
    r"(?:са почетком )?у\s+(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2})|\s+часова)\b",
    re.IGNORECASE,
)
_CYRILLIC_MONTHS = (
    "јануара", "фебруара", "марта", "априла", "маја", "јуна",
    "јула", "августа", "септембра", "октобра", "новембра", "децембра",
)
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
_BITEF_DAY = re.compile(r"(?<!\d)([0-9]{1,2})\.([0-9]{1,2})\.(20[0-9]{2})\.(?!\d)")
_BITEF_SCHEDULE = re.compile(
    r"^Program traje od (?P<hour>[0-9]{1,2})(?::(?P<minute>[0-9]{2})|h)\n"
    r"Lokacija:\nBitefArtCafe\n(?P<address>Mitropolita Petra 8)\n[0-9]{5} Beograd$",
    re.IGNORECASE | re.MULTILINE,
)


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
    return _dob_schedule_facts(name, venue, start, basis, schedule_url)


def _dob_schedule_facts(
    name: str, venue: str, start: datetime, basis: str, schedule_url: str,
) -> dict[str, Any]:
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


def cukarica_stage_start(text: str, *, corroborated_year: int | None = None) -> datetime | None:
    """Время одного события на большой сцене из утверждённого сербского текста."""
    return cukarica_venue_start(text, CUKARICA_STAGE, corroborated_year=corroborated_year)


def cukarica_venue_start(
    text: str, venue: re.Pattern[str], *, corroborated_year: int | None = None,
) -> datetime | None:
    """Одно время и явно указанное помещение, без переноса адреса соседней площадки."""
    text = " ".join(text.casefold().split())
    if not venue.search(text) or re.search(r"отказан|одложен|премештен", text):
        return None
    dates = list(_CUKARICA_DATE.finditer(text))
    if len(dates) != 1:
        return None
    match = dates[0]
    try:
        return datetime(
            int(match["year"] or corroborated_year or 0),
            _CYRILLIC_MONTHS.index(match["month"]) + 1, int(match["day"]),
            int(match["hour"]), int(match["minute"] or 0),
        )
    except ValueError:
        return None


def _cukarica_facts(document: EvidenceDocument) -> dict[str, Any] | None:
    name_match = re.search(r"[„«](.+?)[“»]", document.title)
    if not name_match or len(name_match[1]) > 260:
        return None
    name = name_match[1]
    gallery = CUKARICA_GALLERY.search(" ".join(document.body.split())) is not None
    published = (document.published_at.astimezone(ZoneInfo("Europe/Belgrade"))
                 if document.published_at else None)
    if published is None:
        return None
    # Год без явного указания: ближайшая дата в окне анонса (декабрь → январь).
    venue = CUKARICA_GALLERY if gallery else CUKARICA_STAGE
    candidates = {
        start for year in (published.year, published.year + 1)
        if (start := cukarica_venue_start(document.body, venue, corroborated_year=year))
        is not None
        and timedelta(0) <= start - published.replace(tzinfo=None) <= timedelta(days=120)
    }
    if len(candidates) != 1:
        return None
    start = candidates.pop()
    return {
        "event_name": name,
        "venue": {
            "name": ("Kulturni centar Čukarica · Galerija Start '06" if gallery else
                     "Kulturni centar Čukarica · Velika scena"),
            "address": "Turgenjevljeva 5, Beograd",
            "latitude": 44.7832396, "longitude": 20.4175165,
        },
        "starts_at": start.isoformat(),
        "ends_at": None,
        "time_basis": "source_cukarica_gallery" if gallery else "source_cukarica_main_stage",
        "schedule_source_url": document.url,
        "coordinate_source_url": "https://www.openstreetmap.org/node/2567920976",
        "venue_source_url": "https://www.kccukarica.rs/srl/kontakt/",
    }


def _kolarac_facts(
    document: EvidenceDocument, source_html: str, profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    program = saved_linked_program(source_html, document.url, document.title, document.body)
    if program is None:
        return None
    program_url, starts_at, hall = program
    try:
        hall_key = " ".join(hall.casefold().split())
        venues = [entry for entry in registered_event_venues(profile)
                  if hall_key in {alias.casefold() for alias in entry["aliases"]}]
        if len(venues) != 1:
            return None
        venue = venues[0]
        location = venue["venue"]
        if not all(is_public_https_url(venue[field]) for field in (
            "coordinate_source_url", "venue_source_url",
        )) or not (44.2 <= location["latitude"] <= 45.2 and 19.8 <= location["longitude"] <= 21.1):
            return None
        return {
            "event_name": document.title, "venue": location,
            "starts_at": starts_at.isoformat(), "ends_at": None,
            "time_basis": "source_kolarac_linked_program", "schedule_source_url": program_url,
            "coordinate_source_url": venue["coordinate_source_url"],
            "venue_source_url": venue["venue_source_url"],
        }
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _bitef_facts(
    document: EvidenceDocument, source_html: str, profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    from relocant_media.collectors.bitefartcafe_events import parse_bitefartcafe_event
    from relocant_media.sources.registry import load_source_file

    try:
        config = load_source_file(DEFAULT_CITIES_ROOT / "belgrade/sources/venue_bitefartcafe.yaml")
        live = parse_bitefartcafe_event(source_html, document.url, config)
        if (document.published_at is not None
                or (live.title, live.body) != (document.title, document.body)
                or _CHANGE.search(f"{live.title}\n{live.body}")):
            return None
        days = {tuple(map(int, day)) for day in _BITEF_DAY.findall(live.body)}
        schedules = list(_BITEF_SCHEDULE.finditer(live.body))
        if len(days) != 1 or len(schedules) != 1:
            return None
        day, month, year = days.pop()
        schedule = schedules[0]
        start = datetime(year, month, day, int(schedule["hour"]), int(schedule["minute"] or 0))
        venues = [entry for entry in registered_event_venues(profile)
                  if entry["id"] == "bitefartcafe-winter"
                  and schedule["address"].casefold() in entry["aliases"]]
        if len(venues) != 1:
            return None
        venue = venues[0]
        location = venue["venue"]
        if not all(is_public_https_url(venue[field]) for field in (
            "coordinate_source_url", "venue_source_url",
        )) or not (44.2 <= location["latitude"] <= 45.2 and 19.8 <= location["longitude"] <= 21.1):
            return None
        return {
            "event_name": live.title, "venue": location,
            "starts_at": start.isoformat(), "ends_at": None,
            "time_basis": "source_bitefartcafe_program_start",
            "schedule_source_url": document.url,
            "coordinate_source_url": venue["coordinate_source_url"],
            "venue_source_url": venue["venue_source_url"],
        }
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _linked_tour_facts(document: EvidenceDocument, source_html: str) -> dict[str, Any] | None:
    try:
        place = parse_tour_place(source_html, document.url)
    except ValueError:
        return None
    if (place.title, place.body, None) != (document.title, document.body, document.published_at):
        return None
    return {
        "event_name": place.title,
        "venue": {
            "name": place.venue, "address": place.address,
            "latitude": place.latitude, "longitude": place.longitude,
        },
        "starts_at": place.start.isoformat(), "ends_at": None,
        "time_basis": "source_linked_tour_program_start",
        "schedule_source_url": document.url, "coordinate_source_url": document.url,
        "venue_source_url": document.url,
    }


def _ticket_venue_facts(
    document: EvidenceDocument, source_html: str, profile: CityEditorialProfile,
    media: tuple[EvidenceDocument, ...],
) -> dict[str, Any] | None:
    try:
        config = load_source_file(
            DEFAULT_CITIES_ROOT / profile.city_id / "sources" / f"{TICKET_SOURCE}.yaml",
        )
        ticket = parse_ticket_event(source_html, document.url, config)
        if (ticket.title, ticket.body, None) != (
            document.title, document.body, document.published_at,
        ):
            return None
        stamp = re.fullmatch(
            r"Datum događaja: (\d{2}\.\d{2}\.20\d{2})\.\n"
            r"Početak: (\d{2}:\d{2})\.\nMesto: (.+), Beograd\.", ticket.body,
        )
        if stamp is None:
            return None
        start = datetime.strptime(f"{stamp[1]} {stamp[2]}", "%d.%m.%Y %H:%M")
        timezone = ZoneInfo(profile.timezone)
        matched: list[str] = []
        for article in media:
            if article.published_at is None:
                continue
            item = Collected(article.source_id, article.url, article.url,
                             article.title, article.body, article.published_at)
            if matches_venue_evidence(
                item, ticket, config, published=article.published_at.astimezone(timezone).date(),
            ):
                lead = folded(f"{article.title}\n{article.body[:2500]}")
                matched.extend(
                    clause for clause in re.split(r"[\n;!?]|(?<=[a-z])\.\s+", lead)
                    if event_dates(clause, start.year) == {start.date().isoformat()}
                )
        venues = [entry for entry in registered_event_venues(profile)
                  if folded(stamp[3]) in entry.get("ticket_names", []) and any(
                      re.search(r"(?<!\w)" + re.escape(folded(alias)) + r"(?!\w)", lead)
                      for alias in entry["aliases"] for lead in matched
                  )]
        if len(venues) != 1:
            return None
        venue = venues[0]
        location = venue["venue"]
        if (not all(is_public_https_url(venue[key])
                    for key in ("coordinate_source_url", "venue_source_url"))
                or not 44.2 <= location["latitude"] <= 45.2
                or not 19.8 <= location["longitude"] <= 21.1):
            return None
        return {
            "event_name": document.title, "venue": location,
            "starts_at": start.isoformat(), "ends_at": None,
            "time_basis": "source_cited_ticket_registered_venue",
            "schedule_source_url": document.url,
            "coordinate_source_url": venue["coordinate_source_url"],
            "venue_source_url": venue["venue_source_url"],
        }
    except (OSError, ValueError, TypeError, KeyError):
        return None


def extract_event_adapter(
    document: EvidenceDocument, source_html: str, *, profile: CityEditorialProfile,
    media: tuple[EvidenceDocument, ...] = (),
) -> dict[str, Any] | None:
    """Выделяет только явно датированное событие с подтверждённой площадкой."""
    if (profile.city_id != "belgrade" or not isinstance(source_html, str)
            or len(source_html.encode()) > MAX_HTML_BYTES):
        return None
    if document.source_id == "venue_kolarac":
        return _kolarac_facts(document, source_html, profile)
    if document.source_id == "venue_bitefartcafe":
        return _bitef_facts(document, source_html, profile)
    if document.source_id == TOUR_MAP_SOURCE:
        return _linked_tour_facts(document, source_html)
    if document.source_id == TICKET_SOURCE:
        return _ticket_venue_facts(document, source_html, profile, media)
    if document.source_id == "cukarica_news":
        from relocant_media.collectors.cukarica_news import (
            CukaricaExtractionError,
            parse_cukarica_article,
        )
        from relocant_media.sources.registry import load_source_file

        try:
            source = load_source_file(DEFAULT_CITIES_ROOT / "belgrade/sources/cukarica_news.yaml")
            item = parse_cukarica_article(source_html, source, document.url)
        except CukaricaExtractionError:
            return None
        if (item.url, item.title, item.body) == (document.url, document.title, document.body):
            return _cukarica_facts(document) or festival_program(document, profile=profile)
        return None
    if document.source_id != "venue_youth_centre" or not _dob_url(document.url):
        return None
    soup = BeautifulSoup(source_html, "html.parser")
    canonical = _tag(soup, 'head link[rel="canonical"][href]')
    if canonical is None or canonical.get("href") != document.url:
        return None
    return _dob_facts(document, soup)
