"""Telegram-native presentation helpers that keep visible posts free of raw source URLs."""

from __future__ import annotations

import re
from html import escape, unescape
from urllib.parse import urlsplit

SOURCE_LABEL = "Источник"
TELEGRAM_TEXT_LIMIT = 4096
_SOURCE_LINE = re.compile(r"(?m)^Источник:[ \t]*(https://\S+?)[ \t]*$")
_EDITORIAL_LABEL = re.compile(
    r"^(Где|Когда|Что изменится|Площадка|Вход|Билеты|Запись|Кому|"
    r"Стоимость|Объезд|Питьевая вода|Рубрика):", re.MULTILINE,
)
_NOTICE_LABEL = re.compile(
    r"^(Где|Когда|Что изменится|Объезд|Питьевая вода|Рубрика):", re.MULTILINE,
)
_TIME_RANGE = re.compile(r"\b\d{1,2}:\d{2}[ \t]*[–—-][ \t]*\d{1,2}:\d{2}\b")
_EDITORIAL_TIME = re.compile(r"\b\d{1,2}:\d{2}(?:[ \t]*[–—-][ \t]*\d{1,2}:\d{2})?\b")


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


def editorial_html_parts(
    title: str, body: str, source_url: str, *, notice: bool = False,
) -> tuple[list[str], list[str]]:
    """Разметка без изменения проверенных формулировок."""
    _validate_source_url(source_url)
    labels = _NOTICE_LABEL if notice else _EDITORIAL_LABEL
    times = _TIME_RANGE if notice else _EDITORIAL_TIME
    main, separator, rubric = body.rpartition("\n\nРубрика: ")
    parts = [f"<b>{escape(title)}</b>"]
    for part in escape(main if separator else body).split("\n\n"):
        if part.startswith("«") and part.endswith("»"):
            parts.append(f"<blockquote>{part}</blockquote>")
        else:
            lines = labels.sub(r"<b>\1</b>:", part)
            parts.append(times.sub(r"<code>\g<0></code>", lines))
    source = f'<a href="{escape(source_url, quote=True)}">{SOURCE_LABEL}</a>'
    footer = [f"<b>Рубрика</b>: {escape(rubric)}", source] if separator else [source]
    return parts, footer


def editorial_message_html(text: str) -> tuple[str, str] | None:
    visible, entities = compact_source_link(text)
    footer = f"\n\n{SOURCE_LABEL}"
    if not entities or not visible.endswith(footer):
        return None
    title, separator, body = visible.removesuffix(footer).partition("\n\n")
    source = entities[0]["url"]
    if not separator or not isinstance(source, str):
        return None
    parts, end = editorial_html_parts(title, body, source)
    return "\n\n".join([*parts, *end]), source


def editorial_caption(text: str) -> tuple[str, tuple[dict[str, object], ...]]:
    """Применить ту же визуальную структуру к уже проверенной подписи фотографии."""
    visible, source = compact_source_link(text)
    entities = list(source)

    def mark(kind: str, start: int, end: int) -> None:
        entities.append({"type": kind, "offset": _utf16_units(visible[:start]),
                         "length": _utf16_units(visible[start:end])})

    title, separator, body = visible.partition("\n\n")
    if separator and title:
        mark("bold", 0, len(title))
        position = len(title) + len(separator)
        for part in body.split("\n\n"):
            if part.startswith("«") and part.endswith("»"):
                mark("blockquote", position, position + len(part))
            else:
                for label in _EDITORIAL_LABEL.finditer(part):
                    mark("bold", position + label.start(1), position + label.end(1))
                for hour in _EDITORIAL_TIME.finditer(part):
                    mark("code", position + hour.start(), position + hour.end())
            position += len(part) + 2
    return visible, tuple(entities)


def formatted_text_units(html: str) -> int:
    return _utf16_units(unescape(re.sub(r"<[^>]+>", "", html)))


def rich_editorial_html(html: str) -> str:
    paragraphs = []
    for paragraph in html.split("\n\n"):
        paragraph = paragraph.replace("\n", "<br>")
        paragraphs.append(paragraph if paragraph.startswith("<blockquote") else
                          f"<p>{paragraph}</p>")
    return "".join(paragraphs)
