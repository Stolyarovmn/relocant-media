"""Telegram-native presentation helpers that keep visible posts free of raw source URLs."""

from __future__ import annotations

from urllib.parse import urlsplit

SOURCE_LABEL = "Источник"


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def source_link_entities(text: str, source_url: str | None) -> tuple[dict[str, object], ...]:
    """Link the final visible source label without embedding a raw URL in the text."""
    if source_url is None:
        return ()
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
