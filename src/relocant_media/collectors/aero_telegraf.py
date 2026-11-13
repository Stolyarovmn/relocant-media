"""Датированные материалы авиационного издания; не доказательство от авиакомпании."""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

SOURCE_ID = "media_aero_telegraf"
INDEX = "https://aero.telegraf.rs/avio-kompanije"
_PATH = re.compile(r"/(?:avio-kompanije|aerodromi)/([1-9]\d*)-[a-z0-9-]+\Z")
_MAX_BYTES = 2_000_000


def article_id(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.scheme != "https" or parts.netloc != "aero.telegraf.rs"
            or parts.query or parts.fragment):
        return None
    match = _PATH.fullmatch(parts.path)
    return match[1] if match else None


def _local(title: str) -> bool:
    return bool(re.search(r"\b(?:Air Serbia|Er Srbija|Beograd\w*|Belgrade)\b", title, re.I))


def parse_article(html: str, url: str) -> RawItem:
    identity = article_id(url)
    if identity is None or len(html.encode()) > _MAX_BYTES:
        raise ValueError("invalid aviation article")
    soup = BeautifulSoup(html, "html.parser")
    headline = soup.select_one("article.single-article h1") or soup.find("h1")
    content = soup.select_one("article.single-article .article-content")
    stamp = soup.select_one('meta[property="article:published_time"]')
    canonical = soup.select_one('link[rel="canonical"]')
    if not headline or not content or not stamp or not canonical or canonical.get("href") != url:
        raise ValueError("aviation article identity, content or publication time missing")
    title = " ".join(headline.stripped_strings)
    published = datetime.fromisoformat(str(stamp.get("content", "")))
    paragraphs = [" ".join(node.stripped_strings)
                  for node in content.find_all("p", recursive=False)]
    body = "\n\n".join(row for row in paragraphs if row)
    if not _local(title) or len(body) < 120 or published.utcoffset() is None:
        raise ValueError("aviation article is not local, dated or complete")
    return RawItem(SOURCE_ID, identity, url, title, body, published.astimezone(UTC),
                   source_html=html, published_at_precision="instant")


class AeroTelegrafCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.id != SOURCE_ID or str(source.url) != INDEX:
            raise ValueError("aviation source registry differs")
        self.client = client

    async def _read(self, url: str) -> str:
        data = bytearray()
        async with self.client.stream("GET", url, follow_redirects=False, timeout=10) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > _MAX_BYTES:
                    raise ValueError("aviation response too large")
        return data.decode()

    async def collect_url(self, url: str) -> RawItem:
        if article_id(url) is None:
            raise ValueError("aviation URL outside article scope")
        return parse_article(await self._read(url), url)

    async def collect(self) -> list[RawItem]:
        soup = BeautifulSoup(await self._read(INDEX), "html.parser")
        urls: dict[str, str] = {}
        for a in soup.select("a[href][title]"):
            url = urljoin(INDEX, str(a.get("href", "")))
            key = article_id(url)
            if key and _local(str(a.get("title", ""))):
                urls.setdefault(key, url)
        now = datetime.now(UTC)
        items = []
        for url in list(urls.values())[:5]:
            try:
                item = await self.collect_url(url)
            except (httpx.HTTPError, ValueError):
                continue
            if item.published_at and now - timedelta(hours=48) <= item.published_at <= now:
                items.append(item)
        return items
