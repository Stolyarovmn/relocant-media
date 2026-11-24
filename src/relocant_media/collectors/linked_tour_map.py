"""Подтверждённая организатором пара афиша — билетная карта, отдельная от даты публикации."""

import re
from calendar import month_abbr
from dataclasses import dataclass
from datetime import date, datetime, time
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

SOURCE_ID = "platinumlist_tour_maps"
STRATEGY = "platinumlist_tour_map"
MAX_BYTES = 2_000_000
_MAP_URL = re.compile(r"https://serbia\.platinumlist\.net/event-tickets/[1-9]\d*/[a-z0-9-]+")
_ORGANIZER_URL = re.compile(r"https://eventation\.pro/[a-z0-9-]+")
ORGANIZER_INDEX = "https://eventation.pro/"
_MONTHS = {label.casefold(): month for month, label in enumerate(month_abbr) if label}
_DATE = re.compile(r"([A-Za-z]{3}) (\d{1,2})\s+Belgrade, Serbia\s+(.+?)\s+(20\d{2})\Z")
_CLOCK = re.compile(r"Start:\s*([0-2]\d:[0-5]\d)\Z")
_VENUE_SUFFIX = re.compile(r"\s+in Belgrade\Z", re.I)
_CHANGED = re.compile(r"\b(?:cancel|postpon|reschedul|otkaz|odloz|pomer)\w*", re.I)


class TourMapUnavailable(ValueError):
    """The program or its actual map is unavailable or changed."""


@dataclass(frozen=True, slots=True)
class TourStop:
    map_ticket: str
    title: str
    day: date
    venue: str


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


def supported_tour_map_url(url: str) -> bool:
    return _MAP_URL.fullmatch(url) is not None


def performers(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[^\W\d_]{4,}", _VENUE_SUFFIX.sub("", text).casefold())) - {
        "with", "special", "guests", "live", "tour", "belgrade",
    }


def matching_tour_title(first: str, second: str) -> bool:
    left, right = performers(first), performers(second)
    return bool(left and right and (left <= right or right <= left))


def organizer_pages(html: str, title: str) -> tuple[str, ...]:
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select('link[rel="canonical"][href]')
    if len(canonical) != 1 or canonical[0].get("href") not in {
        ORGANIZER_INDEX, ORGANIZER_INDEX.rstrip("/"),
    }:
        raise ValueError("wrong canonical organizer index")
    pages: set[str] = set()
    for node in soup.select(".t-rec"):
        text = node.get_text(" ", strip=True)
        if not re.search(r"\bBelgrade\b", text, re.I):
            continue
        headings = (tag.get_text(" ", strip=True) for tag in
                    node.select('[data-elem-type="text"] .tn-atom'))
        if not any(matching_tour_title(title, heading) for heading in headings):
            continue
        pages.update(
            url for tag in node.select("a[href]")
            if _ORGANIZER_URL.fullmatch(url := urljoin(ORGANIZER_INDEX, str(tag["href"])))
        )
    if len(pages) > 3:
        raise ValueError("ambiguous organizer calendar")
    return tuple(sorted(pages))


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
    venue_label = _one_text(soup, ".location-block__venue-name")
    venue = _VENUE_SUFFIX.sub("", venue_label)
    address = _one_text(soup, ".location-block__address")
    if not (address.endswith((", Beograd, Serbia", ", Belgrade, Serbia"))
            or address.endswith(", Serbia") and _VENUE_SUFFIX.search(venue_label)):
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


def organizer_stops(html: str, url: str, title: str) -> list[TourStop]:
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select('link[rel="canonical"][href]')
    heading = _one_text(soup, "title")
    if (len(canonical) != 1 or canonical[0].get("href") != url or _CHANGED.search(heading)
            or not matching_tour_title(heading, title)):
        raise ValueError("organizer announces a different event")
    stops = []
    for node in soup.select(".t-rec a[href]"):
        ticket_url = str(node["href"])
        if not supported_tour_map_url(ticket_url):
            continue
        parent = node.find_parent(class_="t-rec")
        if parent is None:
            continue
        text = parent.get_text(" ", strip=True)
        day = _DATE.search(text)
        if day is not None and not _CHANGED.search(text):
            stops.append(TourStop(ticket_url, heading, date(
                int(day[4]), _MONTHS.get(day[1].casefold(), 0), int(day[2]),
            ), day[3]))
    return stops


def _verified_place(place: TourPlace, stops: list[TourStop]) -> bool:
    return len(stops) == 1 and (stops[0].day, stops[0].venue.casefold()) == (
        place.start.date(), place.venue.casefold(),
    ) and matching_tour_title(stops[0].title, place.title)


class LinkedTourMapCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        self.source = source
        self.client = client
        self.index: str | None = None

    async def collect(self) -> list[RawItem]:
        return []

    async def _read(self, url: str) -> str:
        content = bytearray()
        async with self.client.stream("GET", url, follow_redirects=False, timeout=15) as result:
            if result.is_redirect:
                raise TourMapUnavailable("tour source redirected")
            result.raise_for_status()
            async for chunk in result.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise ValueError("tour page is too large")
            return content.decode(result.encoding or "utf-8")

    async def _stops(self, title: str) -> list[TourStop]:
        if self.index is None:
            self.index = await self._read(ORGANIZER_INDEX)
        stops = []
        for organizer in organizer_pages(self.index, title):
            stops.extend(organizer_stops(await self._read(organizer), organizer, title))
        return stops

    def _item(self, url: str, place: TourPlace, html: str) -> RawItem:
        return RawItem(
            source_id=self.source.id, external_id=urlsplit(url).path.split("/")[2],
            url=url, title=place.title, body=place.body, published_at=None, source_html=html,
        )

    async def collect_url(self, url: str) -> RawItem:
        if not supported_tour_map_url(url):
            raise TourMapUnavailable("unsupported tour map address")
        try:
            html = await self._read(url)
            place = parse_tour_place(html, url)
            stops = [stop for stop in await self._stops(place.title) if stop.map_ticket == url]
            if not _verified_place(place, stops):
                raise ValueError("seller details differ from organizer")
            return self._item(url, place, html)
        except ValueError as error:
            raise TourMapUnavailable("tour map could not be verified") from error

    async def collect_for_ticket(self, ticket: RawItem) -> RawItem | None:
        match = re.match(r"^Datum događaja: (\d{2}\.\d{2}\.20\d{2})\.", ticket.body)
        if match is None:
            return None
        day = datetime.strptime(match[1], "%d.%m.%Y").date()
        matches = [stop for stop in await self._stops(ticket.title) if stop.day == day]
        if len(matches) != 1:
            return None
        stop = matches[0]
        html = await self._read(stop.map_ticket)
        place = parse_tour_place(html, stop.map_ticket)
        return self._item(stop.map_ticket, place, html) if _verified_place(place, matches) else None
