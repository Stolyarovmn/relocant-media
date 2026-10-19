import re
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

ARTICLE_ID_RE = re.compile(
    r"/lat/(?:beoinfo-vesti|servisne-informacije/saobracaj-i-prevoz)/"
    r"a(?P<article_id>\d+)/[^/?#]+\.html"
)
DATE_RE = re.compile(r"\b(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})\.")
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")


class ArticleExtractionError(ValueError):
    pass


def match_beograd_article_url(url: str) -> re.Match[str] | None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"www.beograd.rs", "beograd.rs"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return None
    except ValueError:
        return None
    return ARTICLE_ID_RE.fullmatch(parsed.path)


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
    # The city's Nuxt template puts the heading and body in sibling story blocks.
    # Bind the body to this heading rather than falling back to the entire page.
    document = soup.find(None, attrs={"document-title": " ".join(h1.stripped_strings)})
    if isinstance(document, Tag):
        content = document.find_parent(class_="story__full-content")
        return content if isinstance(content, Tag) else document
    article = h1.find_parent("article")
    if isinstance(article, Tag):
        return article
    main = h1.find_parent("main")
    if isinstance(main, Tag):
        return main
    return soup


def _paragraphs_after_heading(root: Tag | BeautifulSoup, h1: Tag) -> list[str]:
    paragraphs: list[str] = []
    following = {id(paragraph) for paragraph in h1.find_all_next("p")}
    for paragraph in root.find_all("p"):
        if not isinstance(paragraph, Tag):
            continue
        if id(paragraph) not in following:
            continue
        if paragraph.find_parent(["footer", "nav", "aside", "form"]) is not None:
            continue
        text = " ".join(paragraph.stripped_strings)
        if text:
            paragraphs.append(text)
    return paragraphs


def parse_beograd_article(html: str, source: SourceConfig, url: str) -> RawItem:
    match = match_beograd_article_url(url)
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
    date = h1.find_next(class_="story__time")
    root_text = date.get_text(" ", strip=True) if isinstance(date, Tag) else " ".join(
        root.stripped_strings
    )

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
        if match_beograd_article_url(url) is None:
            raise ArticleExtractionError(f"unsupported article url: {url}")
        response = await self._client.get(url, follow_redirects=True)
        response.raise_for_status()
        return parse_beograd_article(response.text, self._source, str(response.url))
