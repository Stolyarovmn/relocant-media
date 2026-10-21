"""Telegram-native presentation helpers that keep visible posts free of raw source URLs."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

SOURCE_LABEL = "Источник"
_SOURCE_LINE = re.compile(r"(?m)^Источник:\s*(https://\S+)\s*$")


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _validate_source_url(source_url: str) -> None:
    try:
        parsed = urlsplit(source_url)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("source link must be a public HTTPS URL")


def source_link_entities(text: str, source_url: str | None) -> tuple[dict[str, object], ...]:
    """Link the final visible source label without embedding a raw URL in the text."""
    if source_url is None:
        return ()
    _validate_source_url(source_url)
    start = text.rfind(SOURCE_LABEL)
    if start < 0:
        raise ValueError("source label is missing from Telegram text")
    return (
        {
            "type": "text_link",
            "offset": _utf16_units(text[:start]),
            "length": _utf16_units(SOURCE_LABEL),
            "url": source_url,
        },
    )


def compact_source_link(text: str) -> tuple[str, tuple[dict[str, object], ...]]:
    """Hide exactly one delivery-appended raw source URL behind the visible source label."""
    matches = list(_SOURCE_LINE.finditer(text))
    if not matches:
        return text, ()
    if len(matches) != 1:
        raise ValueError("Telegram text contains ambiguous source links")
    match = matches[0]
    source_url = match.group(1)
    _validate_source_url(source_url)
    visible = text[: match.start()] + SOURCE_LABEL + text[match.end() :]
    return visible, source_link_entities(visible, source_url)
