"""Public Telegram pointers explicitly forwarded by a reader."""

import re
from typing import Any

_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_PUBLIC_POST = re.compile(r"https://t\.me/([a-z][a-z0-9_]{4,31})/([1-9][0-9]{0,15})")
_MAX_MESSAGE_ID = 2**53 - 1


def public_post_ref(url: str) -> tuple[str, int] | None:
    match = _PUBLIC_POST.fullmatch(url)
    if match is None or (message_id := int(match[2])) > _MAX_MESSAGE_ID:
        return None
    return match[1], message_id


def is_forwarded_message(message: dict[str, Any]) -> bool:
    origin = message.get("forward_origin")
    return isinstance(origin, dict) and origin.get("type") in (
        "channel", "chat", "user", "hidden_user",
    )


def forwarded_public_post(message: dict[str, Any]) -> str | None:
    origin = message.get("forward_origin")
    if not isinstance(origin, dict) or origin.get("type") != "channel":
        return None
    chat = origin.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "channel":
        return None
    username = chat.get("username")
    message_id = origin.get("message_id")
    if (not isinstance(username, str) or _USERNAME.fullmatch(username) is None
            or type(message_id) is not int or not 0 < message_id <= _MAX_MESSAGE_ID):
        return None
    return f"https://t.me/{username.lower()}/{message_id}"


def public_forward_urls(payload: dict[str, Any]) -> list[str]:
    parts = payload.get("reader_parts")
    if not isinstance(parts, dict):
        return []
    keys = sorted(
        (key for key in parts if isinstance(key, str) and re.fullmatch(r"[1-9][0-9]{0,15}", key)),
        key=int,
    )
    urls: dict[str, None] = {}
    for key in keys:
        part = parts[key]
        url = part.get("public_forward_url") if isinstance(part, dict) else None
        if isinstance(url, str) and public_post_ref(url) is not None:
            urls[url] = None
    return list(urls)


def has_forwarded_parts(payload: dict[str, Any]) -> bool:
    parts = payload.get("reader_parts")
    return isinstance(parts, dict) and any(
        isinstance(part, dict) and part.get("forwarded") is True for part in parts.values()
    )
