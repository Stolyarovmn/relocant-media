"""Dated street and lane restrictions, from Belgrade's Secretariat for Traffic."""

import asyncio
import re
from datetime import datetime, time, timedelta
from urllib.parse import quote, unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import ArticleBatchError, RawItem, article_failure
from relocant_media.sources.registry import SourceConfig

SOURCE_ID = "bgsaobracaj_notices"
_HOSTS = {"www.bgsaobracaj.rs", "bgsaobracaj.rs"}
_ARTICLE = re.compile(r"/(?:index\.php/)?vest/([1-9][0-9]{0,11})/([\w-]+)/?")
_DEPARTMENT = re.compile(r"/(?:index\.php/)?vesti/16/[\w-]+/?")
_ZONE = ZoneInfo("Europe/Belgrade")
_MAX_ARTICLES = 16
_PARAGRAPH_TAGS = ["p", "li", "tr"]


def _site_path(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in _HOSTS
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 443}):
        raise ValueError("unsupported traffic secretariat host")
    return unquote(parts.path)


def _article_url(url: str) -> tuple[str, str]:
    match = _ARTICLE.fullmatch(_site_path(url))
    if match is None:
        raise ValueError("unsupported traffic secretariat article path")
    article_id, slug = match.groups()
    return f"https://www.bgsaobracaj.rs/vest/{article_id}/{quote(slug, safe='-')}", article_id


def match_bgsaobracaj_notice(url: str) -> bool:
    try:
        _article_url(url)
    except ValueError:
        return False
    return True


def _department_link(link: Tag | None, base: str) -> bool:
    href = link.get("href") if link else None
    if not isinstance(href, str):
        return False
    try:
        return _DEPARTMENT.fullmatch(_site_path(urljoin(base, href))) is not None
    except ValueError:
        return False


def _text(element: Tag) -> str:
    return " ".join(element.get_text(" ", strip=True).replace("\u200b", "").split())


def parse_notice_index(html: str, source: SourceConfig) -> list[RawItem]:
    base = str(source.url)
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("article.civic-news-card")
    if not cards or len(cards) > 100:
        raise ValueError("traffic secretariat cards missing or unexpected")
    entries: dict[str, RawItem] = {}
    for card in cards:
        if not _department_link(card.select_one("a.civic-news-card__department"), base):
            continue
        label = card.select_one(".civic-news-card__title a")
        date_tag = card.select_one("time.civic-news-card__date")
        href = label.get("href") if label else None
        day = date_tag.get("datetime") if date_tag else None
        if not label or not _text(label) or not isinstance(href, str) or not isinstance(day, str):
            raise ValueError("incomplete traffic secretariat card")
        url, article_id = _article_url(urljoin(base, href))
        published = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_ZONE)
        item = RawItem(source.id, article_id, url, _text(label), "", published,
                       published_at_precision="day")
        if article_id in entries and entries[article_id] != item:
            raise ValueError("conflicting traffic secretariat article ID")
        entries[article_id] = item
    return list(entries.values())


def parse_notice_article(html: str, source: SourceConfig, url: str) -> RawItem:
    canonical, article_id = _article_url(url)
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.select(".entry-title h2")
    bodies = soup.select(".entry-content.article-editor-content")
    metadata = soup.select("ul.entry-meta > li")
    if len(headings) != 1 or len(bodies) != 1 or len(metadata) != 2:
        raise ValueError("traffic secretariat article structure missing or ambiguous")
    if not _department_link(metadata[1].select_one("a"), canonical):
        raise ValueError("traffic secretariat article is from a different department")
    published = datetime.strptime(_text(metadata[0]), "%d/%m/%Y").replace(tzinfo=_ZONE)
    body = bodies[0]
    for chrome in body.select(".entry-image, script, style, nav, footer, aside, form"):
        chrome.decompose()
    paragraphs = [_text(block) for block in body.find_all(_PARAGRAPH_TAGS)
                  if block.find(_PARAGRAPH_TAGS) is None]
    body_text = "\n\n".join(filter(bool, paragraphs)) or _text(body)
    title = _text(headings[0])
    if not title or not body_text:
        raise ValueError("traffic secretariat title or full text missing")
    return RawItem(source.id, article_id, canonical, title, body_text, published,
                   source_html=html, published_at_precision="day")


class TrafficSecretariatCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if _site_path(str(source.url)).rstrip("/") != "/vesti":
            raise ValueError("unsupported traffic secretariat index")
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        self.empty_reason: str | None = None

    async def collect_url(self, url: str) -> RawItem:
        canonical, article_id = _article_url(url)
        response = await self._client.get(canonical, follow_redirects=False)
        response.raise_for_status()
        item = parse_notice_article(response.text, self._source, str(response.url))
        if item.external_id != article_id:
            raise ValueError("traffic secretariat article identity changed")
        return item

    async def collect(self) -> list[RawItem]:
        self.item_failures.clear()
        self.empty_reason = None
        response = await self._client.get(str(self._source.url), follow_redirects=False)
        response.raise_for_status()
        entries = parse_notice_index(response.text, self._source)
        today = datetime.now(_ZONE).date()
        # Map phases can still be active after the separate editorial deadline.
        cutoff = today - timedelta(days=14)
        recent = [entry for entry in entries
                  if entry.published_at and cutoff <= entry.published_at.date() <= today]
        earliest = datetime.combine(cutoff, time.min, tzinfo=_ZONE)
        recent.sort(key=lambda item: (item.published_at or earliest, int(item.external_id)),
                    reverse=True)
        concurrency = asyncio.Semaphore(6)

        async def read_entry(entry: RawItem) -> RawItem | None:
            try:
                async with concurrency, asyncio.timeout(9):
                    return await self.collect_url(entry.url)
            except (httpx.HTTPError, TimeoutError, ValueError) as error:
                self.item_failures.append(article_failure(entry.url, error))
                return None

        read = await asyncio.gather(*(read_entry(entry) for entry in recent[:_MAX_ARTICLES]))
        items = [item for item in read if item is not None]
        if self.item_failures and not items:
            raise ArticleBatchError("traffic secretariat articles unavailable", self.item_failures)
        if not items:
            self.empty_reason = "empty_index"
        return items
