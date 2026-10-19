import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Comment, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

ARTICLE_PATH = re.compile(r"/vest/(?P<id>[1-9]\d*)-[a-z0-9-]+/")
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")


class CukaricaExtractionError(ValueError):
    pass


def _allowed_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in {"www.cukarica.rs", "cukarica.rs"}
                or parts.username is not None or parts.password is not None
                or parts.port not in {None, 443}):
            raise ValueError
    except ValueError as exc:
        raise CukaricaExtractionError("unsupported Cukarica URL") from exc
    return urlunsplit(("https", "www.cukarica.rs", parts.path, "", ""))


def _article_url(url: str) -> tuple[str, str]:
    canonical = _allowed_url(url)
    match = ARTICLE_PATH.fullmatch(urlsplit(canonical).path)
    if match is None:
        raise CukaricaExtractionError("unsupported Cukarica article path")
    return canonical, match.group("id")


def _text(node: Tag) -> str:
    return " ".join(node.stripped_strings)


def parse_cukarica_news_list(html: str, source: SourceConfig) -> list[RawItem]:
    base = _allowed_url(str(source.url))
    soup = BeautifulSoup(html, "html.parser")
    grids = soup.select(".news-grid")
    if len(grids) != 1:
        raise CukaricaExtractionError("news grid not found or ambiguous")
    cards = grids[0].select("a.news-grid-item")
    if not cards:
        raise CukaricaExtractionError("news cards not found")
    items: dict[str, RawItem] = {}
    for card in cards:
        href = card.get("href")
        heading = card.select_one(".news-grid-title")
        date = card.select_one(".news-grid-date")
        if not isinstance(href, str) or heading is None or date is None or not _text(heading):
            raise CukaricaExtractionError("incomplete news card")
        url, article_id = _article_url(urljoin(base, href))
        try:
            published = datetime.strptime(_text(date), "%d.%m.%Y").replace(tzinfo=BELGRADE_TZ)
        except ValueError as exc:
            raise CukaricaExtractionError("invalid news date") from exc
        item = RawItem(source.id, article_id, url, _text(heading), "", published)
        if article_id in items and items[article_id] != item:
            raise CukaricaExtractionError("conflicting duplicate news ID")
        items[article_id] = item
    return list(items.values())


def parse_cukarica_article(html: str, source: SourceConfig, url: str) -> RawItem:
    canonical, article_id = _article_url(url)
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select(".page-full-width")
    if len(roots) != 1:
        raise CukaricaExtractionError("article root not found or ambiguous")
    root = roots[0]
    headings = root.find_all("h1")
    contents = root.select(".page-content")
    if len(headings) != 1 or len(contents) != 1 or not _text(headings[0]):
        raise CukaricaExtractionError("article heading or body missing or ambiguous")
    content = contents[0]
    excerpts = [
        comment for comment in root.find_all(string=lambda value: isinstance(value, Comment))
        if str(comment).strip() == "Excerpt"
    ]
    if len(excerpts) != 1:
        raise CukaricaExtractionError("article excerpt marker missing or ambiguous")
    excerpt = excerpts[0].find_next_sibling("div")
    if (
        not isinstance(excerpt, Tag) or excerpt.parent is not root or not _text(excerpt)
        or excerpt is content or content.parent is not root
        or not any(sibling is content for sibling in excerpt.find_next_siblings())
    ):
        raise CukaricaExtractionError("article excerpt missing")
    parts = [_text(excerpt)]
    body = content.get_text("\n", strip=True)
    if not body:
        raise CukaricaExtractionError("article body missing")
    parts.append(body)
    calendar = headings[0].parent.select(".bi-calendar") if headings[0].parent else []
    if len(calendar) != 1 or calendar[0].parent is None:
        raise CukaricaExtractionError("article publication date missing or ambiguous")
    try:
        published = datetime.strptime(_text(calendar[0].parent), "%d.%m.%Y %H:%M").replace(
            tzinfo=BELGRADE_TZ
        )
    except ValueError as exc:
        raise CukaricaExtractionError("invalid article publication date") from exc
    return RawItem(source.id, article_id, canonical, _text(headings[0]),
                   "\n\n".join(parts), published)


class CukaricaNewsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "cukarica_news":
            raise CukaricaExtractionError("unsupported extraction strategy")
        _allowed_url(str(source.url))
        self._source = source
        self._client = client

    async def _get(self, url: str) -> httpx.Response:
        # Validate every redirect before issuing a request, even with a redirect-enabled client.
        current = _allowed_url(url)
        for _ in range(6):
            response = await self._client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise CukaricaExtractionError("redirect without location")
                current = _allowed_url(urljoin(current, location))
                continue
            response.raise_for_status()
            return response
        raise CukaricaExtractionError("too many redirects")

    async def collect_url(self, url: str) -> RawItem:
        canonical, article_id = _article_url(url)
        response = await self._get(canonical)
        item = parse_cukarica_article(response.text, self._source, str(response.url))
        if item.external_id != article_id:
            raise CukaricaExtractionError("redirect changed article ID")
        return item

    async def collect(self) -> list[RawItem]:
        response = await self._get(str(self._source.url))
        entries = parse_cukarica_news_list(response.text, self._source)
        return [await self.collect_url(item.url) for item in entries]
