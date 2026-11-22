"""Discovery-only Serbian news RSS; media text is never publication evidence."""

import ipaddress
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import ArticleBatchError, article_failure

MAX_BYTES = 2_000_000
MAX_LEADS = 20
NEWS_DISCOVERY_REGISTRY = Path("docs/serbian-news-discovery.json")
_MAX_FAILURES = 20


@dataclass(frozen=True, slots=True)
class NewsDiscoveryFeed:
    source_id: str
    source_url: str
    feed_url: str


@dataclass(frozen=True, slots=True)
class NewsLead:
    source_id: str
    url: str
    published_at: datetime
    observed_at: datetime
    text: str
    outbound_urls: tuple[str, ...]
    requires_independent_verification: bool = True
    publication_evidence: bool = False


def _safe_https(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        valid = (
            parts.scheme == "https"
            and bool(host)
            and parts.username is None
            and parts.password is None
            and parts.port in (None, 443)
            and not parts.fragment
        )
    except ValueError:
        valid = False
        host = ""
    if not valid or host in {"localhost", "metadata.google.internal"}:
        raise ValueError("unsafe discovery URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return url
    raise ValueError("IP discovery host forbidden")


def _same_publisher_host(url: str, feed: NewsDiscoveryFeed) -> str:
    _safe_https(url)
    host = (urlsplit(url).hostname or "").casefold()
    allowed = {
        (urlsplit(feed.source_url).hostname or "").casefold(),
        (urlsplit(feed.feed_url).hostname or "").casefold(),
    }
    normalized = host.removeprefix("www.")
    if not any(normalized == candidate.removeprefix("www.") for candidate in allowed):
        raise ValueError("cross-publisher discovery URL")
    return url


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    try:
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (OverflowError, ValueError):
        return None


def load_verified_news_feeds(path: Path) -> tuple[NewsDiscoveryFeed, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    policy = data.get("policy", {})
    if (
        data.get("purpose") != "serbian_news_discovery_only"
        or policy.get("publication_evidence") is not False
        or policy.get("requires_primary_verification") is not True
    ):
        raise ValueError("unsafe news discovery registry policy")

    result: list[NewsDiscoveryFeed] = []
    for source in data.get("sources", []):
        if not isinstance(source, dict):
            raise ValueError("invalid news discovery source")
        if (
            source.get("role") != "discovery_only"
            or source.get("publication_evidence") is not False
            or source.get("requires_primary_verification") is not True
            or source.get("authoritative_for") != []
        ):
            raise ValueError("news source is not discovery-only")
        source_id = source.get("id")
        source_url = source.get("url")
        if not isinstance(source_id, str) or not isinstance(source_url, str):
            raise ValueError("invalid news discovery identity")
        _safe_https(source_url)
        for item in source.get("feeds", []):
            if not isinstance(item, dict) or item.get("status") != "verified_current":
                continue
            feed_url = item.get("url")
            if not isinstance(feed_url, str):
                raise ValueError("invalid verified feed URL")
            feed = NewsDiscoveryFeed(source_id, source_url, feed_url)
            _same_publisher_host(feed_url, feed)
            result.append(feed)
    return tuple(result)


def parse_news_feed(
    xml: str,
    feed: NewsDiscoveryFeed,
    now: datetime,
    *,
    max_age: timedelta = timedelta(hours=24),
    observation: dict[str, Any] | None = None,
) -> list[NewsLead]:
    if len(xml.encode("utf-8")) > MAX_BYTES or any(
        marker in xml.upper() for marker in ("<!DOCTYPE", "<!ENTITY")
    ):
        raise ValueError("unsafe news discovery feed")
    parsed = feedparser.parse(
        xml.encode("utf-8"), sanitize_html=False, resolve_relative_uris=False
    )
    if parsed.get("bozo") or not parsed.version or not (parsed.feed or parsed.entries):
        raise ValueError("invalid news discovery feed")

    observed = now.astimezone(UTC)
    leads: list[NewsLead] = []
    seen: set[str] = set()
    readable = 0
    failures: list[dict[str, str]] = []
    for index, entry in enumerate(parsed.entries, 1):
        title = str(entry.get("title") or "").strip()
        link = str(entry.get("link") or "").strip()
        published = _date(dict.get(entry, "published"))
        try:
            if not title or not link:
                raise ValueError("missing title or article link")
            if published is None:
                raise ValueError("missing or invalid publication timestamp")
            article_url = _same_publisher_host(urljoin(feed.feed_url, link), feed)
        except ValueError as error:
            if len(failures) < _MAX_FAILURES:
                failures.append(article_failure(
                    feed.feed_url, ValueError(f"news entry {index}: {error}"[:120]),
                ))
            continue
        readable += 1
        if (published > observed or published < observed - max_age
                or article_url in seen or len(leads) >= MAX_LEADS):
            continue

        summary_html = str(entry.get("summary", ""))
        summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
        text = " ".join(part for part in (title, summary) if part)[:8000]
        outgoing: list[str] = []
        for anchor in BeautifulSoup(summary_html, "html.parser").select("a[href]"):
            try:
                href = urljoin(article_url, str(anchor.get("href", "")))
                _safe_https(href)
            except ValueError:
                continue
            if href != article_url and href not in outgoing:
                outgoing.append(href)
        leads.append(
            NewsLead(
                source_id=feed.source_id,
                url=article_url,
                published_at=published,
                observed_at=observed,
                text=text,
                outbound_urls=tuple(outgoing[:10]),
            )
        )
        seen.add(article_url)
    if observation is not None:
        status = ("error" if failures and not readable else "partial" if failures
                  else "ok" if leads else "empty_fresh" if readable else "empty_index")
        observation.update(
            status=status, fetched_items=len(parsed.entries), fresh_items=len(leads),
            item_failures=failures,
        )
    if failures and not readable:
        raise ArticleBatchError("all news discovery entries failed extraction", failures)
    return leads


async def _read_news_feed(
    feed: NewsDiscoveryFeed,
    client: httpx.AsyncClient,
) -> str:
    url = feed.feed_url
    for _ in range(5):
        _same_publisher_host(url, feed)
        async with client.stream(
            "GET",
            url,
            follow_redirects=False,
            timeout=25,
            headers={"User-Agent": "RelocantMedia discovery monitoring/1.0"},
        ) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("discovery redirect without location")
                url = _same_publisher_host(urljoin(url, location), feed)
                continue
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError("news discovery response too large")
            return data.decode(response.encoding or "utf-8")
    raise ValueError("news discovery redirect limit")


async def collect_news_feed(
    feed: NewsDiscoveryFeed,
    client: httpx.AsyncClient,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
    observation: dict[str, Any] | None = None,
) -> list[NewsLead]:
    if observation is not None:
        observation.update(source_id=feed.source_id, url=feed.feed_url, item_failures=[])
    try:
        xml = await _read_news_feed(feed, client)
        return parse_news_feed(
            xml, feed, now or datetime.now(UTC), max_age=max_age, observation=observation,
        )
    except (httpx.HTTPError, ValueError) as error:
        if observation is not None:
            observation.update(
                status="transport_error" if isinstance(error, httpx.TransportError) else "error",
                error_type=type(error).__name__,
            )
        raise
