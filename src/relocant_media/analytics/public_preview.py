"""Read-only observations from public Telegram HTML previews; no account access."""

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from bs4 import BeautifulSoup

USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}", re.ASCII)


class PreviewError(ValueError):
    """A preview could not be collected safely or contains no supported posts."""


def validate_username(value: str) -> str:
    if not USERNAME.fullmatch(value):
        raise PreviewError("Недопустимое имя публичного канала")
    return value


@dataclass(frozen=True)
class VisibleCounter:
    value: int
    approximate: bool
    raw: str


def parse_counter(raw: str) -> VisibleCounter | None:
    normalized = raw.strip().replace("\u00a0", "").replace(" ", "")
    match = re.fullmatch(r"([0-9]{1,12}(?:\.[0-9]{1,3})?)([KM]?)", normalized, re.IGNORECASE)
    if not match:
        return None
    number, suffix = match.groups()
    if not suffix and "." in number:
        return None
    multiplier = {"": 1, "K": 1000, "M": 1000000}[suffix.upper()]
    return VisibleCounter(int(Decimal(number) * multiplier), bool(suffix), raw.strip())


def parse_preview(html: str, channel: str, fetched_at: datetime) -> dict[str, Any]:
    validate_username(channel)
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise PreviewError("Время получения должно содержать часовой пояс")
    soup = BeautifulSoup(html, "html.parser")
    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in soup.select(".tgme_widget_message[data-post]"):
        identity = str(node.get("data-post", ""))
        match = re.fullmatch(re.escape(channel) + r"/([1-9]\d*)", identity, re.IGNORECASE)
        if not match or identity.lower() in seen:
            continue
        timestamp = node.select_one("time[datetime]")
        if timestamp is None:
            continue
        try:
            published = datetime.fromisoformat(str(timestamp.get("datetime")))
            if published.tzinfo is None or published.utcoffset() is None:
                continue
        except ValueError:
            continue
        text = node.select_one(".tgme_widget_message_text")
        views_node = node.select_one(".tgme_widget_message_views")
        views = parse_counter(views_node.get_text()) if views_node else None
        posts.append(
            {
                "post_id": int(match.group(1)),
                "url": f"https://t.me/{channel}/{match.group(1)}",
                "published_at": published.astimezone(UTC).isoformat(),
                "text": text.get_text("\n", strip=True) if text else None,
                "views": asdict(views) if views else None,
                "reactions": None,
                "comments": None,
                "forwards": None,
            }
        )
        seen.add(identity.lower())
    if not posts:
        raise PreviewError("Публичный просмотр не содержит поддерживаемых постов")
    return {
        "schema_version": 1,
        "source": "telegram_public_preview",
        "channel": channel,
        "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        "posts": posts,
    }


def collect_preview(channel: str) -> dict[str, Any]:
    validate_username(channel)
    try:
        # Redirects are deliberately disabled: fetch only the validated public endpoint.
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = client.get(f"https://t.me/s/{channel}")
            response.raise_for_status()
    except httpx.HTTPError:
        raise PreviewError("Не удалось получить публичный просмотр Telegram") from None
    return parse_preview(response.text, channel, datetime.now(UTC))
