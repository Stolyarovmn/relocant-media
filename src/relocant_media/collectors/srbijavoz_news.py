"""Structured item-level collector for the official Srbijavoz news archive."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
ARTICLE_PATH = re.compile(r"^/(?P<slug>[a-z0-9][a-z0-9-]{2,199})/$")
VISIBLE_DATE_RE = re.compile(
    r"\b(?P<day>[1-9]|[12]\d|3[01])\.\s*"
    r"(?P<month>[A-Za-zČĆŽŠĐčćžšđА-Яа-я]+)\s+"
    r"(?P<year>20\d{2})\."
)
MONTHS = {
    "januar": 1,
    "februar": 2,
    "mart": 3,
    "april": 4,
    "maj": 5,
    "jun": 6,
    "jul": 7,
    "avgust": 8,
    "septembar": 9,
    "oktobar": 10,
    "novembar": 11,
    "decembar": 12,
    "јануар": 1,
    "фебруар": 2,
    "март": 3,
    "април": 4,
    "мај": 5,
    "јун": 6,
    "јул": 7,
    "август": 8,
    "септембар": 9,
    "октобар": 10,
    "новембар": 11,
    "децембар": 12,
}
RESERVED_SLUGS = {
    "vesti",
    "informacije",
    "opsti-uslovi",
    "rezervisanje",
    "prodajna-mesta",
    "red-voznje",
    "prodaja-karata",
    "kontakt",
    "o-nama",
}
CONTENT_SELECTORS = (
    "[itemprop='articleBody']",
    ".elementor-widget-theme-post-content",
    ".entry-content",
    ".post-content",
)


class SrbijavozExtractionError(ValueError):
    pass


def _text(node: Tag) -> str:
    return " ".join(node.stripped_strings)


def _allowed_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https"
            or parts.hostname not in {"srbijavoz.rs", "www.srbijavoz.rs"}
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
        ):
            raise ValueError
    except ValueError as error:
        raise SrbijavozExtractionError("unsupported Srbijavoz URL") from error
    return urlunsplit(("https", "srbijavoz.rs", parts.path or "/", "", ""))


def _article_url(value: str) -> tuple[str, str]:
    canonical = _allowed_url(value)
    match = ARTICLE_PATH.fullmatch(urlsplit(canonical).path)
    if match is None or match.group("slug") in RESERVED_SLUGS:
        raise SrbijavozExtractionError("unsupported Srbijavoz article path")
    return canonical, match.group("slug")


def _heading_anchor(heading: Tag) -> Tag | None:
    child = heading.find("a", href=True)
    if isinstance(child, Tag):
        return child
    parent = heading.parent
    if isinstance(parent, Tag) and parent.name == "a" and isinstance(parent.get("href"), str):
        return parent
    return None


def parse_srbijavoz_news_list(html: str, source: SourceConfig) -> list[RawItem]:
    """Extract only explicit linked news headings; full articles provide editorial evidence."""
    base = _allowed_url(str(source.url))
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, RawItem] = {}
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if not isinstance(heading, Tag):
            continue
        anchor = _heading_anchor(heading)
        if anchor is None:
            continue
        href = anchor.get("href")
        title = _text(heading)
        if not isinstance(href, str) or not title:
            continue
        try:
            url, slug = _article_url(urljoin(base, href))
        except SrbijavozExtractionError:
            continue
        item = RawItem(source.id, slug, url, title, "", None)
        previous = items.get(slug)
        if previous is not None and previous != item:
            raise SrbijavozExtractionError("conflicting duplicate Srbijavoz news item")
        items[slug] = item
    if not items:
        raise SrbijavozExtractionError("Srbijavoz news headings not found")
    if len(items) > 50:
        raise SrbijavozExtractionError("unexpected Srbijavoz news item count")
    return list(items.values())


def _canonical_from_document(soup: BeautifulSoup, response_url: str) -> tuple[str, str]:
    links = [
        link
        for link in soup.find_all("link", href=True)
        if isinstance(link, Tag)
        and "canonical" in {
            str(value).casefold()
            for value in (
                link.get("rel") if isinstance(link.get("rel"), list) else [link.get("rel")]
            )
        }
    ]
    if len(links) > 1:
        raise SrbijavozExtractionError("ambiguous Srbijavoz canonical URL")
    value = links[0].get("href") if links else response_url
    if not isinstance(value, str):
        raise SrbijavozExtractionError("invalid Srbijavoz canonical URL")
    return _article_url(value)


def _title(soup: BeautifulSoup) -> str:
    values = {text for node in soup.find_all("h1") if isinstance(node, Tag) if (text := _text(node))}
    if len(values) != 1:
        raise SrbijavozExtractionError("article heading missing or ambiguous")
    return values.pop()


def _parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SrbijavozExtractionError("invalid article publication timestamp") from error
    if parsed.utcoffset() is None:
        raise SrbijavozExtractionError("article publication timestamp must be timezone-aware")
    return parsed.astimezone(BELGRADE_TZ)


def _published_at(soup: BeautifulSoup) -> datetime:
    meta_values = {
        str(meta.get("content")).strip()
        for meta in soup.find_all("meta", attrs={"property": "article:published_time"})
        if isinstance(meta, Tag) and isinstance(meta.get("content"), str) and str(meta.get("content")).strip()
    }
    if len(meta_values) > 1:
        raise SrbijavozExtractionError("ambiguous article publication timestamp")
    if meta_values:
        return _parse_iso_datetime(meta_values.pop())

    match = VISIBLE_DATE_RE.search(" ".join(soup.stripped_strings))
    if match is None:
        raise SrbijavozExtractionError("article publication date not found")
    month = MONTHS.get(match.group("month").casefold())
    if month is None:
        raise SrbijavozExtractionError("unsupported article publication month")
    try:
        return datetime(
            int(match.group("year")),
            month,
            int(match.group("day")),
            tzinfo=BELGRADE_TZ,
        )
    except ValueError as error:
        raise SrbijavozExtractionError("invalid article publication date") from error


def _body(soup: BeautifulSoup, title: str) -> str:
    candidates: list[Tag] = []
    seen: set[int] = set()
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector):
            if not isinstance(node, Tag) or id(node) in seen:
                continue
            seen.add(id(node))
            if len(_text(node)) >= 40:
                candidates.append(node)
    if candidates:
        non_nested = [
            node
            for node in candidates
            if not any(parent in candidates for parent in node.parents if isinstance(parent, Tag))
        ]
        candidates = non_nested or candidates
        if len(candidates) != 1:
            raise SrbijavozExtractionError("article content root ambiguous")
        root = candidates[0]
    else:
        articles = [
            node
            for node in soup.find_all("article")
            if isinstance(node, Tag) and len(_text(node)) >= 40
        ]
        if len(articles) != 1:
            raise SrbijavozExtractionError("article content root not found or ambiguous")
        root = articles[0]

    parts: list[str] = []
    for node in root.find_all(["p", "li"]):
        if not isinstance(node, Tag):
            continue
        value = _text(node)
        if value and value != title and value not in parts:
            parts.append(value)
    body = "\n\n".join(parts).strip()
    if len(body) < 40:
        raise SrbijavozExtractionError("article body missing")
    return body


def parse_srbijavoz_article(html: str, source: SourceConfig, response_url: str) -> RawItem:
    response_canonical, response_slug = _article_url(response_url)
    soup = BeautifulSoup(html, "html.parser")
    canonical, slug = _canonical_from_document(soup, response_canonical)
    if slug != response_slug:
        raise SrbijavozExtractionError("article canonical changed identity")
    title = _title(soup)
    return RawItem(
        source_id=source.id,
        external_id=slug,
        url=canonical,
        title=title,
        body=_body(soup, title),
        published_at=_published_at(soup),
        canonical_url=canonical,
        source_html=html,
    )


class SrbijavozNewsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "srbijavoz_news":
            raise SrbijavozExtractionError("unsupported extraction strategy")
        _allowed_url(str(source.url))
        self._source = source
        self._client = client

    async def _get(self, value: str) -> httpx.Response:
        current = _allowed_url(value)
        for _ in range(6):
            response = await self._client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise SrbijavozExtractionError("redirect without location")
                current = _allowed_url(urljoin(current, location))
                continue
            response.raise_for_status()
            return response
        raise SrbijavozExtractionError("too many redirects")

    async def collect_url(self, value: str) -> RawItem:
        expected_url, expected_slug = _article_url(value)
        response = await self._get(expected_url)
        item = parse_srbijavoz_article(response.text, self._source, str(response.url))
        if item.external_id != expected_slug:
            raise SrbijavozExtractionError("redirect changed article identity")
        return item

    async def collect(self) -> list[RawItem]:
        response = await self._get(str(self._source.url))
        entries = parse_srbijavoz_news_list(response.text, self._source)
        return [await self.collect_url(item.url) for item in entries]
