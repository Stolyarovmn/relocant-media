"""Точная привязка заранее опубликованной официальной программы к свежему анонсу."""

from __future__ import annotations

import re
from datetime import datetime

MARATHON_SOURCE_ID = "venue_belgrade_marathon"
MARATHON_PAGE_URL = "https://bgdmarathon.org/07-sava-osiguranje-beogradski-polumaraton-2026/"
_MARATHON_REPORT = re.compile(
    r"\bsedmi beogradski polumaraton,?\s+koji će se održati 22\. novembra\b",
    re.IGNORECASE,
)
_MARATHON_PROGRAM = re.compile(
    r"\bdatum održavanja:\s*nedelja,\s*22\.\s+novembar\s+2026\b",
    re.IGNORECASE,
)


def linked_event_program(
    source_id: str, url: str, body: str, published_at: datetime | None,
) -> tuple[str, str] | None:
    """Реестровый URL, то же издание забега и та же календарная дата."""
    if (
        source_id == "media_danas"
        and url == "https://www.danas.rs/sport/beogradski-polumaraton-humanitarna-akcija/"
        and published_at is not None
        and published_at.year == 2026
        and published_at.month == 11
        and published_at.day == 18
        and _MARATHON_REPORT.search(body)
    ):
        return MARATHON_SOURCE_ID, MARATHON_PAGE_URL
    return None


def confirms_event_program(source_id: str, url: str, body: str) -> bool:
    return (
        source_id == MARATHON_SOURCE_ID
        and url == MARATHON_PAGE_URL
        and _MARATHON_PROGRAM.search(body) is not None
    )
