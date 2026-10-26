"""Public channel topic pointers, deliberately separate from publication evidence."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.analytics.public_preview import (
    PreviewError,
    VisibleCounter,
    parse_preview,
    validate_username,
)

# Runtime messenger discovery is explicitly scoped by city. Serbia-wide channels may still
# be useful to Belgrade, but a source dedicated to another city must never be added to this
# tuple. Future cities receive their own tuple before runtime collection is enabled.
DEFAULT_CHANNELS_BY_CITY: dict[str, tuple[str, ...]] = {
    "belgrade": (
        "Serbia",
        "SerbiaInMyMind",
        "serbia_news",
        "serbnews",
        "novostiserbii",
        "beogradske_vesti",
        "ruserbia",
        "sta_imas_beograd",
        "relocate_serbia",
        "volna_srbjia",
        "vstrechi_v_belgrade",
        "afisha_rs",
        "serbialive_beograd",
        "ylcutcTMU01kNWVk",
    ),
}


def default_channels_for_city(city: str) -> tuple[str, ...]:
    """Return only explicitly enabled runtime channels; unknown cities fail closed."""
    return DEFAULT_CHANNELS_BY_CITY.get(city, ())


def messenger_sources_for_city(
    city: str, registry: Path = Path("docs/messenger-discovery.json"),
) -> list[dict[str, Any]]:
    """Read configured metadata only; this never grants private message access."""
    payload = json.loads(registry.read_text(encoding="utf-8"))
    return [row for row in payload["sources"] if city in row.get("scope", [])]


def runtime_channels_for_city(
    city: str, registry: Path = Path("docs/messenger-discovery.json"),
) -> tuple[str, ...]:
    """Union approved previews with city defaults; catalog-only entries stay disabled."""
    if city not in DEFAULT_CHANNELS_BY_CITY:
        return ()
    channels = list(default_channels_for_city(city))
    for row in messenger_sources_for_city(city, registry):
        collection = row.get("collection", {})
        if row.get("platform") != "telegram" or collection.get("status") != (
            "runtime_public_preview"
        ):
            continue
        parsed = urlsplit(row["url"])
        match = re.fullmatch(r"/s/([A-Za-z][A-Za-z0-9_]{4,31})/?", parsed.path)
        if (parsed.scheme != "https" or parsed.netloc != "t.me" or not match
                or parsed.query or parsed.fragment
                or collection.get("adapter") != "telegram_public_preview"
                or collection.get("account_required") is not False):
            raise PreviewError("Недопустимая конфигурация публичного Telegram-источника")
        channels.append(match.group(1))
    unique: dict[str, str] = {}
    for channel in channels:
        unique.setdefault(channel.lower(), channel)
    return tuple(unique.values())


# Backward-compatible Belgrade alias for the existing Belgrade-only server cycle.
DEFAULT_CHANNELS = default_channels_for_city("belgrade")


@dataclass(frozen=True, slots=True)
class TopicLead:
    channel: str
    message_id: int
    url: str
    published_at: datetime
    observed_at: datetime
    text: str
    outbound_urls: tuple[str, ...]
    views: VisibleCounter | None
    # A Telegram lead is never an independent factual confirmation or RawItem.
    requires_independent_verification: bool = True


def parse_topics(
    html: str, channel: str, now: datetime, *, max_age: timedelta = timedelta(hours=24)
) -> list[TopicLead]:
    preview = parse_preview(html, channel, now, max_age=max_age)
    soup = BeautifulSoup(html, "html.parser")
    nodes = {
        str(node.get("data-post", "")).lower(): node
        for node in soup.select(".tgme_widget_message[data-post]")
    }
    leads = []
    for post in preview["posts"]:
        if not post["text"]:
            continue
        node = nodes.get(f'{channel}/{post["post_id"]}'.lower())
        links: list[str] = []
        if node is not None:
            for anchor in node.select(".tgme_widget_message_text a[href]"):
                href = str(anchor.get("href", ""))
                try:
                    parsed = urlsplit(href)
                    valid = (
                        parsed.scheme == "https" and parsed.hostname
                        and not parsed.username and not parsed.password
                        and parsed.port in (None, 443)
                    )
                except ValueError:
                    continue
                if valid and href not in links:
                    links.append(href)
        counter = post["views"]
        leads.append(TopicLead(
            channel=channel, message_id=post["post_id"], url=post["url"],
            published_at=datetime.fromisoformat(post["published_at"]),
            observed_at=now.astimezone(UTC), text=post["text"],
            outbound_urls=tuple(links), views=VisibleCounter(**counter) if counter else None,
        ))
    return leads


async def collect_topics(
    channel: str, client: httpx.AsyncClient, *, now: datetime | None = None,
    max_age: timedelta = timedelta(hours=24),
    observation: dict[str, Any] | None = None,
) -> list[TopicLead]:
    validate_username(channel)
    now = now or datetime.now(UTC)
    report = observation if observation is not None else {}
    report.update(channel=channel, observed_at=now.astimezone(UTC).isoformat(),
                  status="access_error", leads=None, publication_evidence=False)
    try:
        response = await client.get(f"https://t.me/s/{channel}", follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise PreviewError("Публичный просмотр превышает допустимый размер")
    except httpx.HTTPError:
        raise PreviewError("Не удалось получить публичные темы Telegram") from None
    # Login/landing HTML is a failure, not a successful empty collection.
    if "tgme_widget_message" not in response.text:
        report["status"] = "landing_only"
        raise PreviewError("Публичный просмотр не содержит поддерживаемой разметки")
    soup = BeautifulSoup(response.text, "html.parser")
    nodes = soup.select(".tgme_widget_message[data-post]")
    unknown = dated = stale = future = 0
    fallback_failures = fallback_attempts = 0
    for node in nodes:
        identity = str(node.get("data-post", ""))
        if not re.fullmatch(re.escape(channel) + r"/[1-9]\d*", identity, re.IGNORECASE):
            continue
        timestamp = node.select_one("time[datetime]")
        published = _publication_time(timestamp.get("datetime") if timestamp else None)
        if published is None and node.select_one(".tgme_widget_message_text") is not None:
            # A visible clock is not a publication date. Try only the same post's
            # public permalink, bounded to twenty reads; never guess today's date.
            if fallback_attempts < 20:
                fallback_attempts += 1
                try:
                    permalink = await client.get(
                        f"https://t.me/{identity}?embed=1", follow_redirects=False, timeout=5,
                    )
                    permalink.raise_for_status()
                    if len(permalink.content) <= 2_000_000:
                        detail = BeautifulSoup(permalink.text, "html.parser")
                        exact = detail.select_one(f'[data-post="{identity}"] time[datetime]')
                        published = _publication_time(exact.get("datetime") if exact else None)
                except httpx.HTTPError:
                    fallback_failures += 1
            if published is not None:
                if timestamp is None:
                    timestamp = soup.new_tag("time")
                    node.append(timestamp)
                timestamp["datetime"] = published.isoformat()
        if published is None:
            unknown += 1
        else:
            dated += 1
            stale += int(now - published > max_age)
            future += int(published > now)
    leads = parse_topics(str(soup), channel, now, max_age=max_age)
    status = ("freshness_unknown" if unknown else "dated_messages" if dated > stale + future
              else "stale_snapshot" if stale else "future_dates" if future
              else "unsupported_preview")
    report.update(status=status, leads=len(leads), dated_messages=dated,
                  freshness_unknown=unknown, stale_messages=stale, future_messages=future,
                  permalink_attempts=fallback_attempts, permalink_failures=fallback_failures)
    return leads


def _publication_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None
