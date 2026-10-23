"""Strict direct-link extractor for official Government of Serbia news articles."""

import re
from dataclasses import replace
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

ARTICLE_PATH = re.compile(r"/vest/(?P<id>[1-9]\d*)(?:/(?P<slug>[a-z0-9-]+)\.php)?")
DATE_RE = re.compile(
    r"[^,\n]{1,80},\s*(?P<day>[1-9]|[12]\d|3[01])\.\s*"
    r"(?P<month>јануар|фебруар|март|април|мај|јун|јул|август|септембар|октобар|новембар|децембар)\s+"
    r"(?P<year>20\d{2})\.",
    re.IGNORECASE,
)
MONTHS = {
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
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
_ALLOWED_HOSTS = {"srbija.gov.rs", "www.srbija.gov.rs"}


class SrbijaGovExtractionError(ValueError):
    pass


def match_srbija_gov_article_url(url: str) -> re.Match[str] | None:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in _ALLOWED_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.query
            or parts.fragment
        ):
            return None
    except ValueError:
        return None
    return ARTICLE_PATH.fullmatch(parts.path)


def canonical_srbija_gov_article_url(url: str) -> tuple[str, str]:
    match = match_srbija_gov_article_url(url)
    if match is None:
        raise SrbijaGovExtractionError("unsupported Government of Serbia article URL")
    parts = urlsplit(url)
    return urlunsplit(("https", "www.srbija.gov.rs", parts.path, "", "")), match.group("id")


def _text(node: Tag) -> str:
    return " ".join(node.stripped_strings)


def _meta_content(soup: BeautifulSoup, property_name: str) -> str | None:
    node = soup.find("meta", attrs={"property": property_name})
    if not isinstance(node, Tag):
        return None
    value = node.get("content")
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _declared_title(soup: BeautifulSoup) -> str | None:
    og_title = _meta_content(soup, "og:title")
    if og_title:
        return og_title
    title = soup.find("title")
    if isinstance(title, Tag):
        value = _text(title)
        return value or None
    return None


def _matches_declared_title(candidate: str, declared: str) -> bool:
    if candidate == declared:
        return True
    for separator in (" | ", " - ", " — "):
        if declared.startswith(candidate + separator):
            return True
    return False


def _article_heading(soup: BeautifulSoup) -> Tag:
    headings = [
        node
        for node in soup.find_all(["h1", "h2", "h3"])
        if isinstance(node, Tag) and _text(node)
    ]
    declared = _declared_title(soup)
    if declared:
        matches = [node for node in headings if _matches_declared_title(_text(node), declared)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SrbijaGovExtractionError("declared article heading is ambiguous")
    if len(headings) == 1:
        return headings[0]
    raise SrbijaGovExtractionError("article heading missing or ambiguous")


def _outside_paragraph(value: NavigableString) -> bool:
    parent = value.parent
    return not isinstance(parent, Tag) or (parent.name != "p" and parent.find_parent("p") is None)


def _nearby_publication_date(heading: Tag) -> datetime:
    # The official page renders the location/publication date adjacent to the story heading.
    # Only text before the article body is eligible: a later event/effective date in prose must
    # never be substituted for a missing publication date.
    strings = list(heading.find_all_previous(string=True, limit=30))
    for value in heading.find_all_next(string=True, limit=30):
        if not isinstance(value, NavigableString) or not _outside_paragraph(value):
            break
        strings.append(value)
    for value in strings:
        match = DATE_RE.search(" ".join(str(value).split()))
        if match is None:
            continue
        month = MONTHS.get(match.group("month").casefold())
        if month is None:
            continue
        try:
            return datetime(
                int(match.group("year")),
                month,
                int(match.group("day")),
                tzinfo=BELGRADE_TZ,
            )
        except ValueError as exc:
            raise SrbijaGovExtractionError("invalid explicit publication date") from exc
    raise SrbijaGovExtractionError("explicit publication date not found near article heading")


def _article_root(heading: Tag, soup: BeautifulSoup) -> Tag | BeautifulSoup:
    article = heading.find_parent("article")
    if isinstance(article, Tag):
        return article
    main = heading.find_parent("main")
    if isinstance(main, Tag):
        return main
    return soup


def parse_srbija_gov_article(html: str, source: SourceConfig, url: str) -> RawItem:
    canonical, article_id = canonical_srbija_gov_article_url(url)
    soup = BeautifulSoup(html, "html.parser")
    heading = _article_heading(soup)
    root = _article_root(heading, soup)
    following = {id(node) for node in heading.find_all_next("p")}
    paragraphs: list[str] = []
    for paragraph in root.find_all("p"):
        if not isinstance(paragraph, Tag) or id(paragraph) not in following:
            continue
        if paragraph.find_parent(["footer", "nav", "aside", "form"]) is not None:
            continue
        text = _text(paragraph)
        if text:
            paragraphs.append(text)
    body = "\n\n".join(paragraphs)
    if len(body) < 120:
        raise SrbijaGovExtractionError("article body missing or unexpectedly short")
    return RawItem(
        source_id=source.id,
        external_id=article_id,
        url=canonical,
        title=_text(heading),
        body=body,
        published_at=_nearby_publication_date(heading),
    )


class SrbijaGovArticleCollector:
    """Fetch exact official article URLs without crawling the government news archive."""

    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "srbija_gov_article":
            raise SrbijaGovExtractionError("unsupported extraction strategy")
        self._source = source
        self._client = client

    async def collect(self) -> list[RawItem]:
        """Probe the one fixed verification article configured for source-health."""
        verification_url = self._source.extraction.feed_url
        if not verification_url:
            raise SrbijaGovExtractionError("verification article URL is missing")
        return [await self.collect_url(verification_url)]

    async def collect_url(self, url: str) -> RawItem:
        current, article_id = canonical_srbija_gov_article_url(url)
        for _ in range(6):
            response = await self._client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise SrbijaGovExtractionError("redirect without location")
                current, redirected_id = canonical_srbija_gov_article_url(
                    urljoin(current, location)
                )
                if redirected_id != article_id:
                    raise SrbijaGovExtractionError("redirect changed article ID")
                continue
            response.raise_for_status()
            item = parse_srbija_gov_article(response.text, self._source, current)
            if item.external_id != article_id:
                raise SrbijaGovExtractionError("article ID changed during extraction")
            return replace(item, source_html=None, approved_image_hosts=())
        raise SrbijaGovExtractionError("too many redirects")
