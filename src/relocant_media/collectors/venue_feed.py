"""Bounded first-party RSS/Atom discovery and full-article evidence."""
import ipaddress
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import feedparser
import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 2_000_000
MAX_ITEM_ERROR_MESSAGE = 200


class PosterOnlyEvidence(ValueError):
    """Официальная карточка содержит афишу, но без текстовых сведений."""


def _item_error_message(error: BaseException) -> str:
    """Return bounded single-line diagnostics without page content or response bodies."""
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _date(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError):
            return None
    return result.astimezone(UTC) if result.tzinfo else None


def _allowed(url: str, source: SourceConfig) -> str:
    parts = urlsplit(url)
    hosts = {urlsplit(str(source.url)).hostname, *source.extraction.approved_hosts}
    if (parts.scheme != "https" or parts.hostname not in hosts or parts.username
            or parts.password or parts.port not in (None, 443) or parts.fragment):
        raise ValueError("unapproved venue URL")
    try:
        ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("IP venue host forbidden")
    if parts.hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("private venue host forbidden")
    return url


def parse_venue_feed(html_xml: str, source: SourceConfig) -> list[RawItem]:
    """Only explicit content:encoded is full feed evidence; never use teasers."""
    if len(html_xml.encode("utf-8")) > MAX_BYTES or any(
        token in html_xml.upper() for token in ("<!DOCTYPE", "<!ENTITY")
    ):
        raise ValueError("unsafe venue feed")
    parsed = feedparser.parse(html_xml.encode("utf-8"), sanitize_html=False,
                              resolve_relative_uris=False)
    if parsed.get("bozo"):
        raise ValueError("invalid venue feed XML")
    result = []
    seen: set[str] = set()
    for entry in parsed.entries:
        title, link = entry.get("title", "").strip(), entry.get("link", "").strip()
        # FeedParserDict may provide compatibility aliases. Read only a real
        # publication field; updated/discovered timestamps are never substitutes.
        published = _date(dict.get(entry, "published"))
        if not title or not link or published is None or published > datetime.now(UTC):
            continue
        try:
            url = _allowed(urljoin(str(source.url), link), source)
        except ValueError:
            continue
        if url in seen:
            continue
        body = ""
        contents = entry.get("content", [])
        if contents:
            body = BeautifulSoup(contents[0].get("value", ""), "html.parser").get_text(
                " ", strip=True)
            if len(body) < 120:
                body = ""
        result.append(RawItem(source.id, url, url, title, body, published))
        seen.add(url)
    return result


def _rts_publication(soup: BeautifulSoup) -> datetime:
    """RTS labels publication first and an optional update after an arrow."""
    element = soup.select_one(".storyInfo .storyDate")
    text = element.get_text(" ", strip=True) if element else ""
    match = re.fullmatch(
        r"[^,\d]+,\s*(\d{2}\.\d{2}\.\d{4}),\s*(\d{2}:\d{2})"
        r"(?:\s*->\s*\d{2}:\d{2})?", text,
    )
    if match is None:
        raise ValueError("RTS article publication date missing or invalid")
    try:
        published = datetime.strptime(" ".join(match.groups()), "%d.%m.%Y %H:%M")
    except ValueError as error:
        raise ValueError("RTS article publication date missing or invalid") from error
    return published.replace(tzinfo=ZoneInfo("Europe/Belgrade")).astimezone(UTC)


def _article(html: str, url: str, source: SourceConfig, fallback: datetime | None) -> RawItem:
    soup = BeautifulSoup(html, "html.parser")
    lead_text = ""
    if source.extraction.article_lead_selectors:
        lead = next((element for selector in source.extraction.article_lead_selectors
                     if (element := soup.select_one(selector)) is not None), None)
        if lead is None or not lead.get_text(" ", strip=True):
            raise ValueError("venue article lacks configured lead")
        lead_text = lead.get_text(" ", strip=True)
    title = ""
    body = ""
    published = None
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes += data["@graph"]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("@type") not in ("Article", "NewsArticle", "BlogPosting"):
                continue
            title = str(node.get("headline") or "")
            body = str(node.get("articleBody") or "")
            published = _date(node.get("datePublished"))
    if not title:
        heading = soup.select_one("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
    if published is None:
        meta = soup.select_one('meta[property="article:published_time"]')
        published = _date(str(meta.get("content", ""))) if meta else None
    if not body:
        for selector in source.extraction.article_selectors:
            article = soup.select_one(selector)
            if article:
                for unwanted in article.select("script,style,nav,header,footer,aside,form"):
                    unwanted.decompose()
                body = article.get_text(" ", strip=True)
                break
    body = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    if len(body) < 120 and source.id == "venue_kolarac" and urlsplit(url).path.startswith(
        "/koncerti/",
    ):
        # Short official concert cards can put the full schedule only in the heading.
        # A "Подробнее" stub without concert details still fails the normal check.
        if (re.search(r"\b\d{1,2}\.\s*\d{1,2}\.\s*20\d{2}\.\s*у\s+\d{1,2}\b", title)
                and re.search(r"\b(?:Програм|Улазнице)\s*:", body, re.IGNORECASE)
                and title not in body):
            body = f"{title}\n\n{body}"
    if lead_text and lead_text not in body:
        body = lead_text + " " + body
    published = _rts_publication(soup) if source.id == "media_rts" else published or fallback
    if not title:
        raise ValueError("venue article title missing")
    if len(body) < 120:
        if (source.id == "venue_kolarac" and urlsplit(url).path.startswith("/predavanja/")
                and body.casefold() in {"", "детаљније", "detaljnije"}
                and soup.select_one(".entry-content img[src]")):
            raise PosterOnlyEvidence("official lecture poster has no textual event evidence")
        raise ValueError("venue article body shorter than 120 characters")
    if published is None:
        raise ValueError("venue article publication date missing")
    if published > datetime.now(UTC):
        raise ValueError("venue article publication date is in the future")
    return RawItem(source.id, url, url, title, body, published)


class VenueFeedCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []

    async def _fetch(self, url: str) -> str:
        for _ in range(5):
            _allowed(url, self._source)
            async with self._client.stream(
                "GET", url, follow_redirects=False, timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"}
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect without location")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("venue response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("venue redirect limit")

    async def _entries(self) -> list[RawItem]:
        url = self._source.extraction.feed_url or str(self._source.url)
        return parse_venue_feed(await self._fetch(url), self._source)

    def _needs_page(self) -> bool:
        extraction = self._source.extraction
        return bool(extraction.article_lead_selectors or extraction.page_text)

    async def _collect_article(self, url: str, entry: RawItem | None) -> RawItem:
        use_feed = bool(entry and entry.body and not self._needs_page())
        keep_html = self._source.ownership == "official" and "events" in self._source.categories
        if use_feed and not keep_html:
            assert entry is not None
            return entry
        try:
            html = await self._fetch(url)
        except (ValueError, httpx.HTTPError):
            if use_feed:
                assert entry is not None
                return entry
            raise
        article = entry if use_feed else _article(
            html, url, self._source, entry.published_at if entry else None,
        )
        assert article is not None
        return replace(article, source_html=html) if keep_html else article

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        now = datetime.now(UTC)
        entries = [entry for entry in await self._entries()
                   if entry.published_at and now - timedelta(days=7) <= entry.published_at <= now]
        items = []
        for entry in entries[:20]:
            try:
                items.append(await self._collect_article(entry.url, entry))
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": entry.url,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            # Kolarac publishes some lecture entries as image-only posters with a
            # generic «Детаљније». An empty text-only batch is valid and keeps
            # diagnostics; these entries never become publication evidence.
            posters_only = all(row["error_type"] == "PosterOnlyEvidence"
                               for row in self.item_failures)
            if not posters_only:
                raise ValueError("all fresh venue articles failed extraction")
        return items

    async def collect_url(self, url: str) -> RawItem:
        _allowed(url, self._source)
        entry = next((item for item in await self._entries() if item.url == url), None)
        return await self._collect_article(url, entry)
