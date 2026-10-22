"""Structured full-article collector for official Bgprevoz news."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
MAX_LIST_BYTES = 2_000_000
MAX_DETAIL_BYTES = 8_000_000
MAX_ITEMS = 30
MAX_ITEM_ERROR_MESSAGE = 200

_NEWS_PATH = re.compile(
    r"^(?:/index\.php)?/vesti/(?P<slug>[^/?#]+?)/?$",
    re.IGNORECASE,
)
_DATE = re.compile(r"\b(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>20\d{2})\b")
_READ_MORE = {"опширније", "opsirnije", "read more"}
_CARD_BOUNDARIES = {"body", "html", "main"}
_STOP_LINES = {
    "Секретаријат за јавни превоз",
    "Sekretarijat za javni prevoz",
    "Градска управа града Београда",
    "Gradska uprava grada Beograda",
}


@dataclass(frozen=True)
class BgprevozNewsCandidate:
    slug: str
    url: str
    title: str
    published_at: datetime


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_hostnames(source: SourceConfig) -> set[str]:
    hostname = urlsplit(str(source.url)).hostname
    if not hostname:
        raise ValueError("Bgprevoz news source hostname missing")
    return {hostname, *source.extraction.approved_hosts}


def _allowed_url(value: str, source: SourceConfig) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ValueError("unapproved Bgprevoz news URL") from error
    if (
        parts.scheme.lower() != "https"
        or parts.hostname not in _allowed_hostnames(source)
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise ValueError("unapproved Bgprevoz news URL")
    return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


def _news_identity(value: str, source: SourceConfig) -> tuple[str, str]:
    allowed = _allowed_url(value, source)
    parts = urlsplit(allowed)
    match = _NEWS_PATH.match(parts.path)
    if match is None:
        raise ValueError("Bgprevoz news URL has unexpected path")
    slug = match.group("slug")
    source_parts = urlsplit(str(source.url))
    canonical = urlunsplit(
        (
            "https",
            source_parts.netloc,
            f"/vesti/{slug}",
            "",
            "",
        )
    )
    return slug, canonical


def _parse_card_date(value: str) -> datetime | None:
    match = _DATE.search(value)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=BELGRADE_TZ,
        )
    except ValueError:
        return None


def _ancestor_text_with_date(anchor: Tag) -> str:
    parent = anchor.parent
    current = parent if isinstance(parent, Tag) else None
    for _ in range(6):
        if current is None or current.name in _CARD_BOUNDARIES:
            break
        text = " ".join(current.stripped_strings)
        if _DATE.search(text):
            return text
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return ""


def parse_bgprevoz_news_list(
    html: str,
    source: SourceConfig,
) -> tuple[BgprevozNewsCandidate, ...]:
    """Bind each official detail URL to the date shown on its local index card."""
    if len(html.encode("utf-8")) > MAX_LIST_BYTES:
        raise ValueError("Bgprevoz news list response too large")
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[BgprevozNewsCandidate] = []
    seen: set[str] = set()
    now = datetime.now(BELGRADE_TZ)
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        try:
            slug, canonical = _news_identity(
                urljoin(str(source.url), href.strip()),
                source,
            )
        except ValueError:
            continue
        if slug in seen:
            continue
        title = " ".join(anchor.stripped_strings).strip()
        if not title or title.casefold() in _READ_MORE:
            continue
        card_text = _ancestor_text_with_date(anchor)
        published_at = _parse_card_date(card_text)
        if published_at is None or published_at > now:
            continue
        candidates.append(
            BgprevozNewsCandidate(
                slug=slug,
                url=canonical,
                title=title,
                published_at=published_at,
            )
        )
        seen.add(slug)
        if len(candidates) > MAX_ITEMS:
            raise ValueError("unexpected Bgprevoz news item count")
    if not candidates:
        raise ValueError("Bgprevoz dated news cards not found")
    return tuple(candidates)


def parse_bgprevoz_news_article(
    html: str,
    source: SourceConfig,
    *,
    page_url: str,
    published_at: datetime,
    expected_title: str,
) -> RawItem:
    """Extract full detail text while preserving the exact card publication date."""
    if len(html.encode("utf-8")) > MAX_DETAIL_BYTES:
        raise ValueError("Bgprevoz news detail response too large")
    slug, canonical_url = _news_identity(page_url, source)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading is not None else ""
    if not title:
        raise ValueError("Bgprevoz news title missing")
    if " ".join(title.split()) != " ".join(expected_title.split()):
        raise ValueError("Bgprevoz news title does not match dated index card")

    lines = normalize_visible_text(html).splitlines()
    try:
        title_index = lines.index(title)
    except ValueError as error:
        raise ValueError("Bgprevoz news title missing from visible page") from error

    body_lines: list[str] = []
    for line in lines[title_index + 1 :]:
        if line in _STOP_LINES:
            break
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if len(body) < 120:
        raise ValueError("Bgprevoz news body shorter than 120 characters")

    return RawItem(
        source_id=source.id,
        external_id=f"bgprevoz-news-{slug}",
        url=canonical_url,
        title=title,
        body=body,
        published_at=published_at,
        canonical_url=canonical_url,
        source_html=html,
    )


class BgprevozNewsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "bgprevoz_news":
            raise ValueError(source.extraction.strategy)
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        _allowed_url(str(source.url), source)

    async def _fetch(self, value: str, *, max_bytes: int) -> tuple[str, str]:
        current = _allowed_url(value, self._source)
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
                        raise ValueError("Bgprevoz news redirect without location")
                    current = _allowed_url(urljoin(current, location), self._source)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_bytes = int(content_length)
                    except ValueError:
                        declared_bytes = 0
                    if declared_bytes > max_bytes:
                        raise ValueError("Bgprevoz news response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Bgprevoz news response too large")
                return data.decode(response.encoding or "utf-8"), str(response.url)
        raise ValueError("Bgprevoz news redirect limit")

    async def _candidates(self) -> tuple[BgprevozNewsCandidate, ...]:
        html, _ = await self._fetch(str(self._source.url), max_bytes=MAX_LIST_BYTES)
        return parse_bgprevoz_news_list(html, self._source)

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        items: list[RawItem] = []
        for candidate in await self._candidates():
            try:
                html, final_url = await self._fetch(
                    candidate.url,
                    max_bytes=MAX_DETAIL_BYTES,
                )
                items.append(
                    parse_bgprevoz_news_article(
                        html,
                        self._source,
                        page_url=final_url,
                        published_at=candidate.published_at,
                        expected_title=candidate.title,
                    )
                )
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": candidate.url,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            raise ValueError("all dated Bgprevoz news articles failed extraction")
        return items

    async def collect_url(self, url: str) -> RawItem:
        slug, canonical = _news_identity(url, self._source)
        candidate = next(
            (item for item in await self._candidates() if item.slug == slug),
            None,
        )
        if candidate is None:
            raise ValueError("Bgprevoz news article is not on the dated index page")
        html, final_url = await self._fetch(canonical, max_bytes=MAX_DETAIL_BYTES)
        return parse_bgprevoz_news_article(
            html,
            self._source,
            page_url=final_url,
            published_at=candidate.published_at,
            expected_title=candidate.title,
        )
