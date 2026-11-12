"""Датированные объявления водоканала и HTML точной страницы для карты."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

API = "https://www.bvk.rs/wp-json/wp/v2/posts"
_SLUG = re.compile(r"/[a-z0-9]+(?:-[a-z0-9]+)*/")
_OUTAGE = re.compile(r"(?:прекид|искључ|радов|квар|несташ)\w*.{0,90}вод", re.IGNORECASE)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_FIELDS = "id,slug,link,title,date_gmt"
_MAX_ITEMS = 8
_MAX_BYTES = 2_000_000


def _slug(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc not in {"www.bvk.rs", "bvk.rs"}
            or parts.query or parts.fragment or not _SLUG.fullmatch(parts.path)):
        raise ValueError("unexpected BVK article URL")
    return parts.path.strip("/")


def _title(row: dict[str, Any]) -> str:
    title = row.get("title")
    rendered = title.get("rendered") if isinstance(title, dict) else None
    if not isinstance(rendered, str):
        raise ValueError("BVK article title missing")
    return BeautifulSoup(rendered, "html.parser").get_text(" ", strip=True)


def parse_bvk_outage_article(row: dict[str, Any], html: str, source_id: str) -> RawItem:
    url = row.get("link")
    if (not isinstance(url, str) or row.get("slug") != _slug(url)
            or type(row.get("id")) is not int or row["id"] <= 0):
        raise ValueError("BVK article identity differs from registry")
    stamp = row.get("date_gmt")
    if not isinstance(stamp, str) or not _TIMESTAMP.fullmatch(stamp):
        raise ValueError("BVK publication timestamp missing")
    title = _title(row)
    if not _OUTAGE.search(title) or len(html.encode()) > _MAX_BYTES:
        raise ValueError("BVK record is outside the water outage feed")
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    article = soup.select_one(f"article.post-entry-{row['id']}")
    if canonical is None or canonical.get("href") != url or article is None:
        raise ValueError("BVK article HTML identity differs")
    heading = article.select_one("blockquote.first-quote, h1, h2.post-title")
    entry = article.select_one(".entry-content")
    if (heading is None or " ".join(heading.stripped_strings) != title or entry is None):
        raise ValueError("BVK article title or content differs")
    body = normalize_visible_text(str(entry))
    if len(body) < 60:
        raise ValueError("BVK article text missing")
    return RawItem(source_id=source_id, external_id=f"bvk-post-{row['id']}",
                   url=url, title=title, body=body, source_html=html,
                   published_at=datetime.fromisoformat(stamp).replace(tzinfo=UTC),
                   published_at_precision="instant")


class BvkOutageNewsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client

    async def _get(self, url: str, **params: str) -> bytes:
        content = bytearray()
        async with self._client.stream("GET", url, params=params,
                                      follow_redirects=False, timeout=12) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > _MAX_BYTES:
                    raise ValueError("BVK response exceeds size bound")
        return bytes(content)

    async def _rows(self, **params: str) -> list[dict[str, Any]]:
        rows = json.loads(await self._get(API, _fields=_FIELDS, per_page=str(_MAX_ITEMS), **params))
        if (not isinstance(rows, list) or len(rows) > _MAX_ITEMS
                or any(not isinstance(row, dict) for row in rows)):
            raise ValueError("BVK posts response is malformed")
        return rows

    async def _article(self, row: dict[str, Any]) -> RawItem:
        url = row.get("link")
        if not isinstance(url, str):
            raise ValueError("BVK article URL missing")
        _slug(url)
        return parse_bvk_outage_article(row, (await self._get(url)).decode(), self._source.id)

    async def collect(self) -> list[RawItem]:
        since = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        rows = await self._rows(after=since)
        return [await self._article(row) for row in rows if _OUTAGE.search(_title(row))]

    async def collect_url(self, url: str) -> RawItem:
        rows = await self._rows(slug=_slug(url))
        if len(rows) != 1 or rows[0].get("link") != url:
            raise ValueError("exact BVK article could not be confirmed")
        return await self._article(rows[0])
