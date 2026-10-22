"""Structured evidence from the official Musicology blog."""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import date, datetime
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 3_000_000
MAX_ARTICLES = 5
MAX_ITEM_ERROR_MESSAGE = 200
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
_LIST_PATH = "/blog"
_DETAIL_PATH = re.compile(
    r"^/(?P<year>20\d{2})/(?P<month>0[1-9]|1[0-2])/"
    r"(?P<day>0[1-9]|[12]\d|3[01])/(?P<slug>[a-z0-9][a-z0-9-]*)/?$"
)
_LIST_HEADING = "Blog"
_BODY_STOP = "NEWSLETTER"
_PUBLISHED_META_KEYS = {"article:published_time", "datepublished"}


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_url(value: str, source: SourceConfig, *, detail: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved Musicology URL") from error

    source_url = urlsplit(str(source.url))
    allowed_hosts = {source_url.hostname, *source.extraction.approved_hosts}
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("unapproved Musicology URL")
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("IP Musicology host forbidden")
    if parsed.hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("private Musicology host forbidden")

    path = parsed.path.rstrip("/") or "/"
    if detail:
        if _DETAIL_PATH.fullmatch(path) is None:
            raise ValueError("unapproved Musicology detail path")
    elif path != _LIST_PATH:
        raise ValueError("unapproved Musicology blog path")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _blog_cards(html: str, source: SourceConfig) -> list[tuple[str, str]]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Musicology blog response too large")
    if _LIST_HEADING not in normalize_visible_text(html).splitlines():
        raise ValueError("Musicology blog heading missing")

    soup = BeautifulSoup(html, "html.parser")
    cards: list[tuple[str, str]] = []
    seen: set[str] = set()
    for heading in soup.find_all("h3"):
        if not isinstance(heading, Tag):
            continue
        anchor = heading.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        candidate = urljoin(str(source.url), href.strip())
        try:
            candidate = _allowed_url(candidate, source, detail=True)
        except ValueError:
            continue
        if candidate in seen:
            continue
        title = _normalize_text(anchor.get_text(" ", strip=True))
        if not title:
            raise ValueError("Musicology blog card title missing")
        cards.append((candidate, title))
        seen.add(candidate)
        if len(cards) >= MAX_ARTICLES:
            break

    if not cards:
        raise ValueError("Musicology blog article links missing")
    return cards


def _parse_published_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError("invalid Musicology publication datetime") from error
    if parsed.tzinfo is None:
        raise ValueError("Musicology publication datetime must be timezone-aware")
    return parsed


def _jsonld_published_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "datePublished" and isinstance(child, str):
                yield child
            else:
                yield from _jsonld_published_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _jsonld_published_values(child)


def _published_metadata(soup: BeautifulSoup) -> datetime:
    values: list[str] = []
    for node in soup.find_all("meta"):
        if not isinstance(node, Tag):
            continue
        key = node.get("property") or node.get("name")
        content = node.get("content")
        if (
            isinstance(key, str)
            and key.casefold() in _PUBLISHED_META_KEYS
            and isinstance(content, str)
            and content.strip()
        ):
            values.append(content.strip())

    for node in soup.find_all("script", type="application/ld+json"):
        if not isinstance(node, Tag):
            continue
        raw = node.string or node.get_text()
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values.extend(_jsonld_published_values(payload))

    published = {_parse_published_datetime(value) for value in values}
    if len(published) != 1:
        raise ValueError("Musicology publication metadata missing or ambiguous")
    return next(iter(published))


def _permalink_date(url: str, source: SourceConfig) -> date:
    canonical = _allowed_url(url, source, detail=True)
    match = _DETAIL_PATH.fullmatch(urlsplit(canonical).path.rstrip("/"))
    if match is None:
        raise AssertionError("Musicology detail path invariant")
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as error:
        raise ValueError("invalid Musicology permalink date") from error


def _detail_body(html: str, expected_title: str) -> str:
    visible_lines = [
        _normalize_text(line)
        for line in normalize_visible_text(html).splitlines()
        if _normalize_text(line)
    ]
    title_indexes = [
        index for index, line in enumerate(visible_lines) if line == expected_title
    ]
    if not title_indexes:
        raise ValueError("Musicology article title missing")
    stop_indexes = [
        index for index, line in enumerate(visible_lines) if line == _BODY_STOP
    ]
    if not stop_indexes:
        raise ValueError("Musicology article body boundary missing")

    candidates: list[str] = []
    for title_index in title_indexes:
        stop_index = next(
            (index for index in stop_indexes if index > title_index),
            None,
        )
        if stop_index is None:
            continue
        body_lines = [
            line
            for line in visible_lines[title_index + 1 : stop_index]
            if line != expected_title
        ]
        if body_lines:
            candidates.append("\n".join(body_lines))
    if not candidates:
        raise ValueError("Musicology article body missing")

    body = max(candidates, key=len).strip()
    if len(body) < 120:
        raise ValueError("Musicology article body shorter than 120 characters")
    return body


def parse_musicology_article(
    html: str,
    url: str,
    title: str,
    source: SourceConfig,
) -> RawItem:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Musicology article response too large")
    canonical = _allowed_url(url, source, detail=True)
    soup = BeautifulSoup(html, "html.parser")
    headings = [
        _normalize_text(node.get_text(" ", strip=True))
        for node in soup.find_all("h1")
        if isinstance(node, Tag)
    ]
    if headings.count(title) != 1:
        raise ValueError("Musicology article H1 missing or ambiguous")

    published_at = _published_metadata(soup)
    if published_at.astimezone(BELGRADE_TZ).date() != _permalink_date(canonical, source):
        raise ValueError("Musicology publication metadata disagrees with permalink date")

    body = _detail_body(html, title)
    return RawItem(
        source_id=source.id,
        external_id=canonical,
        url=canonical,
        title=title,
        body=body,
        published_at=published_at,
        canonical_url=canonical,
        source_html=html,
    )


class MusicologyBlogCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "musicology_blog":
            raise ValueError(source.extraction.strategy)
        if source.policy.auto_publish:
            raise ValueError("Musicology blog must stay review-only")
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        _allowed_url(str(source.url), source, detail=False)

    async def _fetch(self, value: str, *, detail: bool) -> str:
        current = _allowed_url(value, self._source, detail=detail)
        for _ in range(6):
            async with self._client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Musicology redirect without location")
                    current = _allowed_url(
                        urljoin(current, location),
                        self._source,
                        detail=detail,
                    )
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > MAX_BYTES:
                        raise ValueError("Musicology response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Musicology response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("Musicology redirect limit")

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        listing = await self._fetch(str(self._source.url), detail=False)
        cards = _blog_cards(listing, self._source)
        items: list[RawItem] = []
        for link, title in cards:
            try:
                items.append(
                    parse_musicology_article(
                        await self._fetch(link, detail=True),
                        link,
                        title,
                        self._source,
                    )
                )
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": link,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            first = self.item_failures[0]
            raise ValueError(
                "all Musicology article details failed extraction: "
                f"{first['error_type']}: {first['error_message']}"
            )
        return sorted(
            items,
            key=lambda item: item.published_at or datetime.min.replace(tzinfo=BELGRADE_TZ),
            reverse=True,
        )
