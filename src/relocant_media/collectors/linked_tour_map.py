"""Подтверждённая организатором пара афиша — билетная карта, отдельная от даты публикации."""

import json
import re
from calendar import month_abbr
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.ticket_vision_events import cited_ticket_urls, ticket_event_url
from relocant_media.sources.registry import SourceConfig

SOURCE_ID = "platinumlist_tour_maps"
STRATEGY = "platinumlist_tour_map"
MAX_BYTES = 2_000_000
_MAP_URL = re.compile(r"https://serbia\.platinumlist\.net/event-tickets/[1-9]\d*/[a-z0-9-]+")
_ORGANIZER_URL = re.compile(r"https://eventation\.pro/[a-z0-9-]+")
_MONTHS = {label.casefold(): month for month, label in enumerate(month_abbr) if label}
_DATE = re.compile(r"([A-Za-z]{3}) (\d{1,2})\s+Belgrade, Serbia\s+(.+?)\s+(20\d{2})\Z")
_CLOCK = re.compile(r"Start:\s*([0-2]\d:[0-5]\d)\Z")
_VENUE_SUFFIX = re.compile(r"\s+in Belgrade\Z", re.I)
_CHANGED = re.compile(r"\b(?:cancel|postpon|reschedul|otkaz|odloz|pomer)\w*", re.I)


class TourMapUnavailable(ValueError):
    """The program or its actual map is unavailable or changed."""


@dataclass(frozen=True, slots=True)
class TourLink:
    article_ticket: str
    organizer: str
    map_ticket: str


@dataclass(frozen=True, slots=True)
class TourPlace:
    title: str
    start: datetime
    venue: str
    address: str
    latitude: float
    longitude: float

    @property
    def body(self) -> str:
        return (f"Datum događaja: {self.start:%d.%m.%Y}.\nPočetak: {self.start:%H:%M}.\n"
                f"Mesto: {self.venue}, Beograd.\nAdresa: {self.address}")


def tour_links(city_root: Path) -> tuple[TourLink, ...]:
    path = city_root / "tour-map-links.json"
    if not path.is_file():
        return ()
    links = tuple(TourLink(**row) for row in json.loads(path.read_text(encoding="utf-8")))
    if (len(links) > 250 or len({link.article_ticket for link in links}) != len(links)
            or len({link.map_ticket for link in links}) != len(links)
            or any(not ticket_event_url(link.article_ticket)
                   or not _MAP_URL.fullmatch(link.map_ticket)
                   or not _ORGANIZER_URL.fullmatch(link.organizer) for link in links)):
        raise ValueError("invalid or duplicate tour map links")
    return links


def registered_tour_map_url(url: str, city_root: Path) -> bool:
    return bool(_MAP_URL.fullmatch(url)) and any(
        link.map_ticket == url for link in tour_links(city_root)
    )


def cited_tour_link(body: str, city_root: Path) -> TourLink | None:
    tickets = cited_ticket_urls(body)
    return next((link for link in tour_links(city_root) if {link.article_ticket} == tickets), None)


def performers(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[^\W\d_]{4,}", _VENUE_SUFFIX.sub("", text).casefold())) - {
        "with", "special", "guests",
    }


def _one_text(soup: BeautifulSoup, selector: str) -> str:
    nodes = soup.select(selector)
    if len(nodes) != 1 or not (value := nodes[0].get_text(" ", strip=True)):
        raise ValueError("tour card contains missing or ambiguous fields")
    return value


def parse_tour_place(html: str, url: str) -> TourPlace:
    if not _MAP_URL.fullmatch(url) or len(html.encode()) > MAX_BYTES:
        raise ValueError("invalid tour card address or size")
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select('link[rel="canonical"][href]')
    if len(canonical) != 1 or canonical[0].get("href") != url:
        raise ValueError("wrong canonical tour card")
    title = _one_text(soup, ".event-item h1.event-item__name-title")
    if len(title) > 180 or _CHANGED.search(title):
        raise ValueError("unusable tour title")
    venue = _VENUE_SUFFIX.sub("", _one_text(soup, ".location-block__venue-name"))
    address = _one_text(soup, ".location-block__address")
    if not address.endswith((", Beograd, Serbia", ", Belgrade, Serbia")):
        raise ValueError("venue outside Belgrade")
    coordinates = soup.select(".location-block a.location-block__view-navigation-button[href]")
    if len(coordinates) != 1:
        raise ValueError("missing or ambiguous venue coordinates")
    map_url = urlsplit(str(coordinates[0]["href"]))
    value = parse_qs(map_url.query).get("q", [])
    if ((map_url.scheme, map_url.netloc, map_url.path) != ("https", "www.google.com", "/maps")
            or len(value) != 1
            or not re.fullmatch(r"[+-]?\d+\.\d+,[+-]?\d+\.\d+", value[0])):
        raise ValueError("map URL does not give exact coordinates")
    latitude, longitude = map(float, value[0].split(","))
    if not (44.2 <= latitude <= 45.2 and 19.8 <= longitude <= 21.1):
        raise ValueError("map coordinates outside Belgrade")
    starts = set()
    for node in soup.select(".buy-block__time-text-wrapper"):
        label = node.get_text(" ", strip=True)
        if label.startswith("Start:"):
            clock = _CLOCK.fullmatch(label)
            if clock is None:
                raise ValueError("invalid program start")
            starts.add(clock[1])
    days = set()
    for node in soup.select("a.add-to-calendar-block__item[href]"):
        calendar = urlsplit(str(node["href"]))
        if (calendar.scheme, calendar.netloc, calendar.path) != (
            "https", "calendar.google.com", "/calendar/render",
        ):
            continue
        query = parse_qs(calendar.query)
        if query.get("ctz") != ["Europe/Belgrade"] or query.get("text") != [title]:
            raise ValueError("calendar entry differs from the tour card")
        dates = query.get("dates", [])
        if len(dates) != 1 or not re.fullmatch(r"20\d{6}T\d{6}/20\d{6}T\d{6}", dates[0]):
            raise ValueError("tour calendar lacks an exact date")
        days.add(datetime.strptime(dates[0][:8], "%Y%m%d").date())
    if len(starts) != 1 or len(days) != 1:
        raise ValueError("missing or ambiguous tour schedule")
    start = datetime.combine(days.pop(), time.fromisoformat(starts.pop()))
    return TourPlace(title, start, venue, address, latitude, longitude)


def verify_organizer(html: str, link: TourLink, place: TourPlace) -> None:
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select('link[rel="canonical"][href]')
    title = _one_text(soup, "title")
    if (len(canonical) != 1 or canonical[0].get("href") != link.organizer
            or len(performers(title)) < 2 or performers(title) != performers(place.title)):
        raise ValueError("organizer announces a different event")
    anchors = [node for node in soup.select("a[href]") if node.get("href") == link.map_ticket]
    parent = anchors[0].find_parent(class_="t-rec") if len(anchors) == 1 else None
    if parent is None:
        raise ValueError("organizer does not link one exact ticket card")
    text = parent.get_text(" ", strip=True)
    day = _DATE.search(text)
    if (day is None or day[3] != place.venue or _CHANGED.search(text) or _CHANGED.search(title)):
        raise ValueError("organizer date or venue differs from the ticket card")
    if date(int(day[4]), _MONTHS.get(day[1].casefold(), 0), int(day[2])) != place.start.date():
        raise ValueError("organizer date or venue differs from the ticket card")


class LinkedTourMapCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient, city_root: Path) -> None:
        self.source = source
        self.client = client
        self.links = {link.map_ticket: link for link in tour_links(city_root)}

    async def collect(self) -> list[RawItem]:
        return []

    async def _read(self, url: str) -> str:
        content = bytearray()
        async with self.client.stream("GET", url, follow_redirects=False, timeout=15) as result:
            result.raise_for_status()
            async for chunk in result.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ValueError("tour page is too large")
            return content.decode(result.encoding or "utf-8")

    async def collect_url(self, url: str) -> RawItem:
        link = self.links.get(url)
        if link is None:
            raise ValueError("unapproved tour map address")
        try:
            html = await self._read(url)
            place = parse_tour_place(html, url)
            verify_organizer(await self._read(link.organizer), link, place)
        except ValueError as error:
            raise TourMapUnavailable("tour map could not be verified") from error
        return RawItem(
            source_id=self.source.id, external_id=urlsplit(url).path.split("/")[2],
            url=url, title=place.title, body=place.body, published_at=None, source_html=html,
        )
