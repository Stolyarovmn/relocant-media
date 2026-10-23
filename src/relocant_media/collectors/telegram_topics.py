"""Public channel topic pointers, deliberately separate from publication evidence."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.analytics.public_preview import (
    PreviewError,
    VisibleCounter,
    parse_preview,
    validate_username,
)

# Public Serbia/Belgrade channels are useful as a discovery surface only.
# Inclusion here does not make a channel trusted evidence and does not change publication policy.
DEFAULT_CHANNELS = (
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
)


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
) -> list[TopicLead]:
    validate_username(channel)
    try:
        response = await client.get(f"https://t.me/s/{channel}", follow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            raise PreviewError("Публичный просмотр превышает допустимый размер")
    except httpx.HTTPError:
        raise PreviewError("Не удалось получить публичные темы Telegram") from None
    # Login/landing HTML is a failure, not a successful empty collection.
    if "tgme_widget_message" not in response.text:
        raise PreviewError("Публичный просмотр не содержит поддерживаемой разметки")
    return parse_topics(response.text, channel, now or datetime.now(UTC), max_age=max_age)
