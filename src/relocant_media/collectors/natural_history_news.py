"""Structured evidence from the official Natural History Museum news pages."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 3_000_000
MAX_ARTICLES = 10
MAX_ITEM_ERROR_MESSAGE = 200
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
_LIST_PATH = "/vesti"
_DETAIL_PATH = re.compile(r"^/(?P<slug>[a-z0-9][a-z0-9-]*)/?$")
_DATE = re.compile(r"^(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>20\d{2})\.$")
_NEWS_HEADING = "Вести"
_READ_MORE = "прочитај више »"


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_url(value: str, source: SourceConfig, *, detail: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved Natural History Museum URL") from error

    source_url = urlsplit(str(source.url))
    allowed_hosts = {source_url.hostname, *source.extraction.approved_hosts}
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("unapproved Natural History Museum URL")
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("IP Natural History Museum host forbidden")
    if parsed.hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("private Natural History Museum host forbidden")

    path = parsed.path.rstrip("/") or "/"
    if detail:
        if _DETAIL_PATH.fullmatch(path) is None or path == _LIST_PATH:
            raise ValueError("unapproved Natural History Museum detail path")
    elif path != _LIST_PATH:
        raise ValueError("unapproved Natural History Museum news path")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))


def _parse_date(value: str) -> datetime:
    match = _DATE.fullmatch(value.strip())
    if match is None:
        raise ValueError("invalid Natural History Museum publication date")
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=BELGRADE_TZ,
        )
    except ValueError as error:
        raise ValueError("invalid Natural History Museum publication date") from error


def _card_metadata(
    heading: Tag,
    candidate: str,
    source: SourceConfig,
) -> tuple[bool, datetime | None]:
    dates: set[datetime] = set()
    read_more_seen = False
    for node in heading.find_all_next():
        if node is not heading and isinstance(node, Tag) and node.name == "h3":
            break
        if not isinstance(node, Tag):
            continue
        text = node.get_text(" ", strip=True)
        if _DATE.fullmatch(text):
            dates.add(_parse_date(text))
        if node.name != "a" or text.casefold() != _READ_MORE:
            continue
        href = node.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        try:
            read_more_url = _allowed_url(
                urljoin(str(source.url), href.strip()),
                source,
                detail=True,
            )
        except ValueError:
            continue
        if read_more_url == candidate:
            read_more_seen = True

    if not read_more_seen:
        return False, None
    if len(dates) != 1:
        raise ValueError("Natural History Museum news card date missing or ambiguous")
    return True, next(iter(dates))


def _news_cards(html: str, source: SourceConfig) -> list[tuple[str, str, datetime]]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Natural History Museum news response too large")
    visible_lines = normalize_visible_text(html).splitlines()
    if _NEWS_HEADING not in visible_lines:
        raise ValueError("Natural History Museum news heading missing")
    soup = BeautifulSoup(html, "html.parser")

    cards: list[tuple[str, str, datetime]] = []
    seen: set[str] = set()
    for heading in soup.find_all("h3"):
        if not isinstance(heading, Tag):
            continue
        anchor = heading.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        candidate = urljoin(str(source.url), href.strip())
        try:
            candidate = _allowed_url(candidate, source, detail=True)
        except ValueError:
            continue
        if candidate in seen:
            continue
        is_card, published_at = _card_metadata(heading, candidate, source)
        if not is_card:
            continue
        if published_at is None:
            raise AssertionError("news card date invariant")
        title = " ".join(anchor.stripped_strings)
        if not title:
            raise ValueError("Natural History Museum news card title missing")
        cards.append((candidate, title, published_at))
        seen.add(candidate)
        if len(cards) >= MAX_ARTICLES:
            break

    if not cards:
        raise ValueError("Natural History Museum news cards missing")
    published = [item[2] for item in cards]
    if published != sorted(published, reverse=True):
        raise ValueError("Natural History Museum publication dates are not descending")
    return cards


def _detail_body(html: str, expected_title: str) -> str:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Natural History Museum article response too large")
    soup = BeautifulSoup(html, "html.parser")
    matching_titles = [
        node
        for node in soup.find_all(["h1", "h2", "h3"])
        if " ".join(node.stripped_strings) == expected_title
    ]
    if len(matching_titles) != 1 or not isinstance(matching_titles[0], Tag):
        raise ValueError("Natural History Museum article title missing or ambiguous")
    title_node = matching_titles[0]

    paragraphs: list[str] = []
    for node in title_node.find_all_next():
        if not isinstance(node, Tag):
            continue
        if node.name == "footer":
            break
        text = node.get_text(" ", strip=True)
        if node.name in {"h2", "h3"} and text.casefold() == "newsletter":
            break
        if node.name != "p" or not text:
            continue
        if not paragraphs or paragraphs[-1] != text:
            paragraphs.append(text)
    body = "\n".join(paragraphs).strip()
    if len(body) < 120:
        raise ValueError("Natural History Museum article body shorter than 120 characters")
    return body


def parse_natural_history_article(
    html: str,
    url: str,
    title: str,
    published_at: datetime,
    source: SourceConfig,
) -> RawItem:
    canonical = _allowed_url(url, source, detail=True)
    if published_at.tzinfo is None:
        raise ValueError("Natural History Museum publication date must be timezone-aware")
    body = _detail_body(html, title)
    return RawItem(
        source_id=source.id,
        external_id=canonical,
        url=canonical,
        title=title,
        body=body,
        published_at=published_at,
        canonical_url=canonical,
        source_html=html,
    )


class NaturalHistoryNewsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "natural_history_news":
            raise ValueError(source.extraction.strategy)
        if source.policy.auto_publish:
            raise ValueError("Natural History Museum news must stay review-only")
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        _allowed_url(str(source.url), source, detail=False)

    async def _fetch(self, value: str, *, detail: bool) -> str:
        current = _allowed_url(value, self._source, detail=detail)
        for _ in range(6):
            async with self._client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Natural History Museum redirect without location")
                    current = _allowed_url(
                        urljoin(current, location),
                        self._source,
                        detail=detail,
                    )
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > MAX_BYTES:
                        raise ValueError("Natural History Museum response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Natural History Museum response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("Natural History Museum redirect limit")

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        listing = await self._fetch(str(self._source.url), detail=False)
        cards = _news_cards(listing, self._source)
        items: list[RawItem] = []
        for link, title, published_at in cards:
            try:
                items.append(
                    parse_natural_history_article(
                        await self._fetch(link, detail=True),
                        link,
                        title,
                        published_at,
                        self._source,
                    )
                )
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": link,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            first = self.item_failures[0]
            raise ValueError(
                "all Natural History Museum article details failed extraction: "
                f"{first['error_type']}: {first['error_message']}"
            )
        return items
