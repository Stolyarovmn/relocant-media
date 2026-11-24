"""Билетная карточка может подтвердить расписание статьи, но не её дату публикации."""

import re
from datetime import date, time
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.domain.event_facts import event_dates
from relocant_media.sources.registry import SourceConfig

STRATEGY = "ticket_vision_event"
SOURCE_ID = "ticket_vision_events"
MAX_BYTES = 512 * 1024
_EVENT_URL = re.compile(r"https://tickets\.rs/event/[a-z0-9_]{1,120}_[1-9][0-9]{0,9}")
_EVENT_LINK = re.compile(r"(?<![\w:/=&])" + _EVENT_URL.pattern + r"(?![\w/=?&#-])")
_DATE_TIME = re.compile(
    r"(?P<day>[0-9]{1,2})\.\s+(?P<month>[^\W\d_]+)\s+(?P<year>20[0-9]{2})"
    r",\s+[^\W\d_]+\s+(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})",
)


class TicketCardUnavailable(ValueError):
    """The exact approved URL did not return a usable canonical event card."""


def ticket_event_url(url: str) -> bool:
    return _EVENT_URL.fullmatch(url) is not None


def cited_ticket_urls(text: str) -> set[str]:
    return {match[0] for match in _EVENT_LINK.finditer(text)}


def parse_ticket_event(html: str, url: str, source: SourceConfig) -> RawItem:
    if not ticket_event_url(url) or len(html.encode()) > MAX_BYTES:
        raise ValueError("invalid ticket event document")
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select('link[rel="canonical"]')
    titles = soup.select("h1")
    selections = soup.select(".selected-event")
    if (len(canonical) != 1 or canonical[0].get("href") != url
            or len(titles) != 1 or len(selections) != 1):
        raise ValueError("ticket event identity is missing or ambiguous")
    title = titles[0].get_text(" ", strip=True)
    values = [token.get_text(" ", strip=True) for token in selections[0].select("p > strong")]
    if len(values) != 3 or values[0] != title or not 1 <= len(title) <= 180:
        raise ValueError("ticket card must contain one title, venue and start")
    place, separator, city = values[1].rpartition(",")
    place, city = place.strip(), city.strip()
    if not separator or city.casefold() not in {"belgrade", "beograd", "београд"} or not place:
        raise ValueError("ticket event is outside Belgrade")
    clock = _DATE_TIME.fullmatch(values[2])
    if clock is None:
        raise ValueError("ticket event lacks a verified start")
    dates = event_dates(f"{clock['day']}. {clock['month']} {clock['year']}", int(clock["year"]))
    if len(dates) != 1:
        raise ValueError("ticket event lacks a verified date")
    day = date.fromisoformat(next(iter(dates)))
    start = time(int(clock["hour"]), int(clock["minute"]))
    return RawItem(
        source_id=source.id, external_id=urlsplit(url).path.rsplit("_", 1)[-1],
        url=url, title=title,
        body=f"Datum događaja: {day:%d.%m.%Y}.\nPočetak: {start:%H:%M}.\nMesto: {place}, Beograd.",
        published_at=None, source_html=html,
    )


class TicketVisionEventCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        self.source = source
        self.client = client

    async def collect(self) -> list[RawItem]:
        # A booking page is read only when a public article cites its exact URL.
        return []

    async def collect_url(self, url: str) -> RawItem:
        if not ticket_event_url(url):
            raise ValueError("unsupported ticket event URL")
        content = bytearray()
        async with self.client.stream("GET", url, follow_redirects=False, timeout=20) as response:
            if response.is_redirect:
                raise TicketCardUnavailable("ticket card redirected")
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise TicketCardUnavailable("ticket card is too large")
            try:
                html = content.decode(response.encoding or "utf-8")
                return parse_ticket_event(html, url, self.source)
            except ValueError as error:
                raise TicketCardUnavailable("ticket card could not be verified") from error
