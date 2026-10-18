import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

ARTICLE_ID_RE = re.compile(r"/lat/beoinfo-vesti/a(?P<article_id>\d+)/[^?#]+\.html(?:[?#].*)?$")
DATE_RE = re.compile(r"\b(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})\.")
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")


class ArticleExtractionError(ValueError):
    pass


def _parse_day_only_date(text: str) -> datetime | None:
    match = DATE_RE.search(text)
    if match is None:
        return None
    return datetime(
        int(match.group("year")),
        int(match.group("month")),
        int(match.group("day")),
        tzinfo=BELGRADE_TZ,
    )


def _article_root(h1: Tag, soup: BeautifulSoup) -> Tag | BeautifulSoup:
    article = h1.find_parent("article")
    if isinstance(article, Tag):
        return article
    main = h1.find_parent("main")
    if isinstance(main, Tag):
        return main
    return soup


def _paragraphs_after_heading(root: Tag | BeautifulSoup, h1: Tag) -> list[str]:
    paragraphs: list[str] = []
    for paragraph in root.find_all("p"):
        if not isinstance(paragraph, Tag):
            continue
        if paragraph.sourceline is not None and h1.sourceline is not None:
            if paragraph.sourceline <= h1.sourceline:
                continue
        text = " ".join(paragraph.stripped_strings)
        if text:
            paragraphs.append(text)
    return paragraphs


def parse_beograd_article(html: str, source: SourceConfig, url: str) -> RawItem:
    match = ARTICLE_ID_RE.search(url)
    if match is None:
        raise ArticleExtractionError(f"unsupported article url: {url}")

    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not isinstance(h1, Tag):
        raise ArticleExtractionError("article h1 not found")

    title = " ".join(h1.stripped_strings)
    if not title:
        raise ArticleExtractionError("article title is empty")

    root = _article_root(h1, soup)
    body_parts = _paragraphs_after_heading(root, h1)
    if not body_parts:
        raise ArticleExtractionError("article body paragraphs not found")

    article_text = "\n\n".join(body_parts)
    root_text = " ".join(root.stripped_strings)

    return RawItem(
        source_id=source.id,
        external_id=match.group("article_id"),
        url=url,
        title=title,
        body=article_text,
        published_at=_parse_day_only_date(root_text),
    )


class BelgradeArticleCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client

    async def collect_url(self, url: str) -> RawItem:
        response = await self._client.get(url, follow_redirects=True)
        response.raise_for_status()
        return parse_beograd_article(response.text, self._source, str(response.url))
