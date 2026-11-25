"""Bounded first-party RSS/Atom discovery and full-article evidence."""
import asyncio
import ipaddress
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.kolarac_program import (
    KOLARAC_SCHEDULE,
    MAX_PROGRAM_BYTES,
    linked_program,
    parse_linked_program,
)
from relocant_media.domain.publication_time import BELGRADE, local_clock_precision
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 2_000_000
MAX_ITEM_ERROR_MESSAGE = 200
_KOLARAC_HALL = re.compile(
    r"(?:велика дворана|velika dvorana|мала сала|mala sala|галерија|galerija)",
    re.IGNORECASE,
)
_POSTER_STUBS = {"", "детаљније", "detaljnije"}
_BIRN_COUNTRIES = {
    "albania", "bosnia and herzegovina", "bulgaria", "croatia", "czechia", "czech republic",
    "greece", "hungary", "kosovo", "moldova", "montenegro", "north macedonia", "poland",
    "romania", "serbia", "slovakia", "turkey",
}
_BIRN_SECTION_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_SERBIAN_SCOPE = re.compile(r"\b(?:serbia|serbian|belgrade|beograd|novi\s+sad)\b", re.I)


class PosterOnlyEvidence(ValueError):
    """Официальная карточка содержит афишу, но без текстовых сведений."""


class OutsideArticleScope(ValueError):
    """У карточки нет заявленной привязки к городу источника."""


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
    if any(parts.path.startswith(prefix) for prefix in source.extraction.excluded_path_prefixes):
        raise ValueError("unsupported source content path")
    return url


def _index_article_url(url: str, source: SourceConfig) -> str:
    index = source.extraction.article_index
    assert index is not None
    parts = urlsplit(_allowed(url, source))
    path = parts.path.rstrip("/")
    if not re.fullmatch(index.path_pattern, path):
        raise ValueError("unsupported article index link")
    # An index article is identified by its path. Language query and host aliases
    # don't identify separate news items.
    return urlunsplit(("https", urlsplit(str(source.url)).netloc, path, "", ""))


def parse_article_index(html: str, source: SourceConfig) -> list[RawItem]:
    index = source.extraction.article_index
    assert index is not None
    soup = BeautifulSoup(html, "html.parser")
    entries: dict[str, RawItem] = {}
    for row in soup.select(index.item_selector):
        link = row.select_one(index.link_selector)
        href = link.get("href") if link else None
        if not isinstance(href, str):
            continue
        stamps = {
            match[0].rstrip(".") for element in row.select(index.date_selector)
            if (match := re.fullmatch(r"\d{2}\.\d{2}\.\d{4}\.?", element.get_text(strip=True)))
        }
        if len(stamps) != 1:
            continue
        try:
            url = _index_article_url(urljoin(str(source.url), href), source)
            day = datetime.strptime(stamps.pop(), "%d.%m.%Y")
        except ValueError:
            continue
        published = day.replace(tzinfo=BELGRADE).astimezone(UTC)
        entries.setdefault(url, RawItem(
            source.id, url, url, "", "", published, published_at_precision="day",
        ))
    if not entries:
        raise ValueError("official article index has no dated article links")
    return sorted(
        entries.values(), key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def _balkan_foreign_only(tags: list[dict[str, object]], context: str) -> bool:
    terms = [" ".join(value.split()) for tag in tags if isinstance(value := tag.get("term"), str)]
    countries = {
        _BIRN_SECTION_SEPARATOR.split(term.casefold(), maxsplit=1)[0] for term in terms
    } & _BIRN_COUNTRIES
    # Явно зарубежные рубрики можно отсеять. Смешанные и неполные рубрики сохраняем.
    return bool(countries and not _SERBIAN_SCOPE.search(" ".join((context, *terms))))


def parse_venue_feed(
    html_xml: str, source: SourceConfig, *, discover: bool = True,
) -> list[RawItem]:
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
        if (discover and source.extraction.feed_title_pattern
                and not re.search(source.extraction.feed_title_pattern, title)):
            continue
        try:
            url = _allowed(urljoin(str(source.url), link), source)
        except ValueError:
            continue
        if url in seen:
            continue
        if (discover and source.id == "media_balkan_insight"
                and _balkan_foreign_only(entry.get("tags", []),
                                         f"{title} {entry.get('summary', '')[:2000]}")):
            continue
        body = ""
        contents = entry.get("content", [])
        if contents:
            body = BeautifulSoup(contents[0].get("value", ""), "html.parser").get_text(
                " ", strip=True)
            if len(body) < 120:
                body = ""
        result.append(RawItem(source.id, url, url, title, body, published,
                              published_at_precision="instant"))
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
    return published.replace(tzinfo=BELGRADE)


def _complete_kolarac_heading(title: str) -> bool:
    schedules = list(KOLARAC_SCHEDULE.finditer(title))
    if len(schedules) != 1:
        return False
    schedule = schedules[0]
    if (not any(char.isalpha() for char in title[:schedule.start()])
            or _KOLARAC_HALL.fullmatch(title[schedule.end():].strip()) is None):
        return False
    try:
        datetime(
            int(schedule["year"]), int(schedule["month"]), int(schedule["day"]),
            int(schedule["hour"]), int(schedule["minute"] or "0"),
        )
    except ValueError:
        return False
    return True


def _article(
    html: str, url: str, source: SourceConfig, fallback: datetime | None,
    *, program: tuple[str, str] | None = None, fallback_precision: str = "instant",
) -> RawItem:
    soup = BeautifulSoup(html, "html.parser")
    if scope := source.extraction.article_scope:
        scope_fragments = [
            element.get_text(" ", strip=True)
            for selector in scope.selectors
            if (element := soup.select_one(selector)) is not None
        ]
        if not any(scope_fragments):
            raise ValueError("venue article locality fields missing")
        if not any(re.search(scope.pattern, text) for text in scope_fragments):
            raise OutsideArticleScope("venue article has no verified city reference")
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
    heading = soup.select_one("h1")
    heading_text = heading.get_text(" ", strip=True) if heading else ""
    title = title or heading_text
    if published is None:
        meta = soup.select_one('meta[property="article:published_time"]')
        published = _date(str(meta.get("content", ""))) if meta else None
    if source.id == "media_n1_srbija" and source.extraction.page_text:
        roots = soup.select("article.article-wrapper")
        if len(roots) != 1:
            raise ValueError("N1 main article missing or ambiguous")
        root = roots[0]
        blocks = root.select(
            ":scope > .article-content-wrapper > .rich-text-block",
        )
        if not any(block.get_text(strip=True) for block in blocks):
            raise ValueError("N1 full article text missing")
        body = " ".join(block.get_text(" ", strip=True) for block in root.select(
            ":scope > p, :scope > .article-content-wrapper > .rich-text-block",
        ))
    if not body:
        for selector in source.extraction.article_selectors:
            article = soup.select_one(selector)
            if article:
                for unwanted in article.select("script,style,nav,header,footer,aside,form"):
                    unwanted.decompose()
                body = article.get_text(" ", strip=True)
                break
    body = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    kolarac = source.id == "venue_kolarac"
    kolarac_path = urlsplit(url).path if kolarac else ""
    complete_schedule = False
    if len(body) < 120 and kolarac_path.startswith("/koncerti/"):
        # Short official concert cards can put the full schedule only in the heading.
        normalized_heading = " ".join(heading_text.split())
        complete_schedule = (
            normalized_heading == " ".join(title.split())
            and _complete_kolarac_heading(normalized_heading)
        )
        if complete_schedule:
            # The official /koncerti/ section confirms the event type. Keep its
            # label with the date so editorial freshness sees an actual concert.
            schedule = f"Концерт: {normalized_heading}"
            if body.casefold() in _POSTER_STUBS or " ".join(body.split()) == normalized_heading:
                body = schedule
            else:
                body = f"{schedule}\n\n{body}"
        elif (title not in body and KOLARAC_SCHEDULE.search(title)
                and re.search(r"\b(?:Програм|Улазнице)\s*:", body, re.IGNORECASE)):
            body = f"{title}\n\n{body}"
    if lead_text and lead_text not in body:
        body = lead_text + " " + body
    precision = "instant"
    if source.id == "media_rts":
        local = _rts_publication(soup)
        precision = local_clock_precision(local)
        published = local.astimezone(UTC)
    elif published is None:
        published = fallback
        precision = fallback_precision
    if program is not None:
        title, body = program
        complete_schedule = True
    if not title:
        raise ValueError("venue article title missing")
    if len(body) < 120 and not complete_schedule:
        if (kolarac_path.startswith(("/koncerti/", "/predavanja/"))
                and body.casefold() in _POSTER_STUBS
                and soup.select_one(".entry-content img[src]")):
            raise PosterOnlyEvidence("official event poster has no sufficient textual schedule")
        raise ValueError("venue article body shorter than 120 characters")
    if published is None:
        raise ValueError("venue article publication date missing")
    if published > datetime.now(UTC):
        raise ValueError("venue article publication date is in the future")
    return RawItem(source.id, url, url, title, body, published, published_at_precision=precision)


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

    async def _entries(self, *, discover: bool = True) -> list[RawItem]:
        url = self._source.extraction.feed_url or str(self._source.url)
        body = await self._fetch(url)
        if self._source.extraction.article_index is not None:
            return parse_article_index(body, self._source)
        return parse_venue_feed(body, self._source, discover=discover)

    async def _program_pdf(self, url: str) -> bytes:
        _allowed(url, self._source)
        async with self._client.stream(
            "GET", url, follow_redirects=False, timeout=15,
            headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
        ) as response:
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").partition(";")[0]
            if media_type.strip().lower() != "application/pdf":
                raise ValueError("Kolarac program is not a PDF response")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_PROGRAM_BYTES:
                    raise ValueError("Kolarac program PDF too large")
            return bytes(data)

    def _needs_page(self) -> bool:
        extraction = self._source.extraction
        return bool(extraction.article_lead_selectors or extraction.article_scope
                    or extraction.page_text)

    async def _collect_article(
        self, url: str, entry: RawItem | None, *, require_page: bool = False,
    ) -> RawItem:
        use_feed = bool(entry and entry.body and not self._needs_page())
        keep_html = self._source.ownership == "official" and "events" in self._source.categories
        if use_feed and not keep_html and not require_page:
            assert entry is not None
            return entry
        try:
            html = await self._fetch(url)
        except (ValueError, httpx.HTTPError):
            if use_feed and not require_page:
                assert entry is not None
                return entry
            raise
        published = entry.published_at if entry else None
        try:
            article = entry if use_feed else _article(
                html, url, self._source, published,
                fallback_precision=entry.published_at_precision if entry else "instant",
            )
        except PosterOnlyEvidence:
            link = linked_program(html, url)
            if link is None:
                raise
            program_url, starts_at = link
            try:
                async with asyncio.timeout(15):
                    document = await self._program_pdf(program_url)
            except TimeoutError as error:
                raise ValueError("Kolarac program download exceeded 15 seconds") from error
            program = parse_linked_program(document, program_url, starts_at)
            article = _article(html, url, self._source, published, program=program)
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
            except OutsideArticleScope:
                continue
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": entry.url,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            # A batch of official posters with no usable textual schedules can
            # legitimately contain no publishable items. Preserve its diagnostics.
            posters_only = all(row["error_type"] == "PosterOnlyEvidence"
                               for row in self.item_failures)
            if not posters_only:
                raise ValueError("all fresh venue articles failed extraction")
        return items

    async def collect_url(self, url: str, *, require_page: bool = False) -> RawItem:
        url = (_index_article_url(url, self._source) if self._source.extraction.article_index
               else _allowed(url, self._source))
        try:
            entry = next((item for item in await self._entries(discover=False) if item.url == url),
                         None)
        except (httpx.HTTPError, ValueError):
            if not require_page:
                raise
            entry = None
        return await self._collect_article(url, entry, require_page=require_page)
