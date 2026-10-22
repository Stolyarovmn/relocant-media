"""Structured high-risk evidence for Belgrade duty health institutions."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.beograd_article import (
    match_beograd_article_url,
    parse_beograd_article,
)
from relocant_media.sources.registry import SourceConfig

BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
MAX_LIST_BYTES = 2_000_000
MAX_DETAIL_BYTES = 4_000_000
MAX_ITEMS = 50
MAX_ITEM_ERROR_MESSAGE = 200
LOOKBACK_DAYS = 7

_DATE_RE = re.compile(r"\b(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>20\d{2})\.")
_DUTY_RE = re.compile(r"dežurne\s+zdravstvene\s+ustanove", re.IGNORECASE)
_HEALTH_PATH = re.compile(
    r"^/lat/servisne-informacije/zdravstvo/a(?P<article_id>\d+)/[^/?#]+\.html$"
)


@dataclass(frozen=True)
class HealthDutyCandidate:
    article_id: str
    url: str
    published_at: datetime


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_hosts(source: SourceConfig) -> set[str]:
    hostname = urlsplit(str(source.url)).hostname
    if not hostname:
        raise ValueError("health source hostname missing")
    return {hostname, *source.extraction.approved_hosts}


def _allowed_url(value: str, source: SourceConfig) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved health source URL") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in _allowed_hosts(source)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("unapproved health source URL")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def _health_identity(value: str, source: SourceConfig) -> tuple[str, str]:
    allowed = _allowed_url(value, source)
    parsed = urlsplit(allowed)
    match = _HEALTH_PATH.fullmatch(parsed.path)
    if match is None or match_beograd_article_url(allowed) is None:
        raise ValueError("health duty article URL has unexpected path")
    source_parts = urlsplit(str(source.url))
    canonical = urlunsplit(("https", source_parts.netloc, parsed.path, "", ""))
    return match.group("article_id"), canonical


def _parse_date(value: str) -> datetime | None:
    match = _DATE_RE.search(value)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=BELGRADE_TZ,
        )
    except ValueError:
        return None


def parse_health_duty_list(
    html: str,
    source: SourceConfig,
) -> tuple[HealthDutyCandidate, ...]:
    """Bind exact official health article URLs to dates shown on their own links."""
    if len(html.encode("utf-8")) > MAX_LIST_BYTES:
        raise ValueError("health duty list response too large")
    soup = BeautifulSoup(html, "html.parser")
    result: list[HealthDutyCandidate] = []
    seen: set[str] = set()
    now = datetime.now(BELGRADE_TZ)
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = anchor.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        text = " ".join(anchor.stripped_strings)
        if not _DUTY_RE.search(text):
            continue
        try:
            article_id, canonical = _health_identity(
                urljoin(str(source.url), href.strip()),
                source,
            )
        except ValueError:
            continue
        published_at = _parse_date(text)
        if published_at is None or published_at > now:
            continue
        if article_id in seen:
            continue
        seen.add(article_id)
        result.append(HealthDutyCandidate(article_id, canonical, published_at))
        if len(result) > MAX_ITEMS:
            raise ValueError("unexpected health duty article count")
    if not result:
        raise ValueError("dated health duty articles not found")
    return tuple(result)


def _validate_detail(
    html: str,
    source: SourceConfig,
    *,
    page_url: str,
    candidate: HealthDutyCandidate,
) -> RawItem:
    if len(html.encode("utf-8")) > MAX_DETAIL_BYTES:
        raise ValueError("health duty detail response too large")
    article_id, canonical = _health_identity(page_url, source)
    if article_id != candidate.article_id:
        raise ValueError("health duty article identity changed")
    item = parse_beograd_article(html, source, canonical)
    if item.published_at != candidate.published_at:
        raise ValueError("health duty detail date does not match dated index link")
    if not _DUTY_RE.search(item.title):
        raise ValueError("health duty detail title is unexpected")
    if len(item.body) < 120:
        raise ValueError("health duty detail body shorter than 120 characters")
    return replace(
        item,
        url=canonical,
        canonical_url=canonical,
        source_html=html,
        approved_image_hosts=("static.beograd.rs",),
    )


class HealthDutyCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "health_duty":
            raise ValueError(source.extraction.strategy)
        if not source.policy.high_risk or source.policy.auto_publish:
            raise ValueError("health duty source must stay high-risk and review-only")
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        _allowed_url(str(source.url), source)

    async def _fetch(self, value: str, *, max_bytes: int) -> tuple[str, str]:
        current = _allowed_url(value, self._source)
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
                        raise ValueError("health source redirect without location")
                    current = _allowed_url(urljoin(current, location), self._source)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > max_bytes:
                        raise ValueError("health source response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("health source response too large")
                return data.decode(response.encoding or "utf-8"), str(response.url)
        raise ValueError("health source redirect limit")

    async def _candidates(self) -> tuple[HealthDutyCandidate, ...]:
        html, _ = await self._fetch(str(self._source.url), max_bytes=MAX_LIST_BYTES)
        return parse_health_duty_list(html, self._source)

    async def _collect_candidate(self, candidate: HealthDutyCandidate) -> RawItem:
        html, final_url = await self._fetch(candidate.url, max_bytes=MAX_DETAIL_BYTES)
        return _validate_detail(
            html,
            self._source,
            page_url=final_url,
            candidate=candidate,
        )

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        cutoff = datetime.now(BELGRADE_TZ) - timedelta(days=LOOKBACK_DAYS)
        candidates = [item for item in await self._candidates() if item.published_at >= cutoff]
        items: list[RawItem] = []
        for candidate in candidates:
            try:
                items.append(await self._collect_candidate(candidate))
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": candidate.url,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            raise ValueError("all recent health duty articles failed extraction")
        return items

    async def collect_url(self, url: str) -> RawItem:
        article_id, _ = _health_identity(url, self._source)
        candidate = next(
            (item for item in await self._candidates() if item.article_id == article_id),
            None,
        )
        if candidate is None:
            raise ValueError("health duty article is not on the dated official index")
        return await self._collect_candidate(candidate)
