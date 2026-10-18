import re
from datetime import datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

ARTICLE_HREF_RE = re.compile(r"/lat/beoinfo-vesti/a(?P<article_id>\d+)/[^?#]+\.html(?:[?#].*)?$")
DATE_RE = re.compile(r"\b(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})\.")
LABEL_RE = re.compile(r"\bBeoinfo\s+vesti\b", re.IGNORECASE)
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")


class UnsupportedExtractionStrategyError(ValueError):
    pass


def _clean_anchor_text(anchor: Tag) -> str:
    text = " ".join(anchor.stripped_strings)
    text = LABEL_RE.sub(" ", text)
    text = DATE_RE.sub(" ", text)
    return " ".join(text.split())


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


def parse_beograd_news_list(html: str, source: SourceConfig) -> list[RawItem]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[RawItem] = []
    seen_ids: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href_value = anchor.get("href")
        if not isinstance(href_value, str):
            continue

        match = ARTICLE_HREF_RE.search(href_value)
        if match is None:
            continue

        article_id = match.group("article_id")
        if article_id in seen_ids:
            continue

        title = _clean_anchor_text(anchor)
        if not title:
            continue

        raw_text = " ".join(anchor.stripped_strings)
        items.append(
            RawItem(
                source_id=source.id,
                external_id=article_id,
                url=urljoin(str(source.url), href_value),
                title=title,
                body=raw_text,
                published_at=_parse_day_only_date(raw_text),
            )
        )
        seen_ids.add(article_id)

    return items


class BelgradeNewsListCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "beograd_news_list":
            raise UnsupportedExtractionStrategyError(source.extraction.strategy)
        self._source = source
        self._client = client

    async def collect(self) -> list[RawItem]:
        response = await self._client.get(str(self._source.url), follow_redirects=True)
        response.raise_for_status()
        return parse_beograd_news_list(response.text, self._source)
