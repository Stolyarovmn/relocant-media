"""Structured extraction for official EDS Belgrade planned-outage day pages."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.domain.dedup import normalize_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 3_000_000
_DATE = re.compile(
    r"Планирана искључења за датум:\s*"
    r"(?P<date>\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}\.)"
)
_TIME = re.compile(r"^\d{2}:\d{2}\s*-\s*\d{2}:\d{2}$")
_HEADER = ("општина", "време", "улице")


def _normalize_schedule_date(value: str) -> str:
    value = value.strip().rstrip(".")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def _notice_id(
    schedule_date: str,
    municipality: str,
    interval: str,
    occurrence: int,
) -> str:
    seed = "\n".join(
        (
            schedule_date,
            normalize_text(municipality),
            normalize_text(interval),
            str(occurrence),
        )
    ).encode()
    return "eds-" + hashlib.sha256(seed).hexdigest()[:24]


def _internal_canonical_url(url: str, external_id: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("__eds_notice", external_id))
    query.sort()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _cells(row: Tag) -> list[str]:
    return [
        " ".join(cell.get_text(" ", strip=True).split())
        for cell in row.find_all(["th", "td"])
    ]


def parse_eds_belgrade_outages(
    html: str,
    source: SourceConfig,
    *,
    page_url: str | None = None,
) -> list[RawItem]:
    """Return one RawItem per official outage row; an empty valid table returns []."""
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("EDS outage response too large")

    visible = normalize_visible_text(html)
    date_match = _DATE.search(visible)
    if date_match is None:
        raise ValueError("EDS schedule date not found")
    schedule_date = _normalize_schedule_date(date_match.group("date"))

    soup = BeautifulSoup(html, "html.parser")
    rows = [row for row in soup.find_all("tr") if isinstance(row, Tag)]
    if not rows:
        raise ValueError("EDS outage table not found")

    parsed_rows = [_cells(row) for row in rows]
    if not any(tuple(value.casefold() for value in cells[:3]) == _HEADER for cells in parsed_rows):
        raise ValueError("EDS outage table header not found")

    public_url = page_url or str(source.url)
    occurrences: defaultdict[tuple[str, str], int] = defaultdict(int)
    items: list[RawItem] = []
    for cells in parsed_rows:
        if len(cells) < 3:
            continue
        municipality, interval, streets = cells[0], cells[1], " ".join(cells[2:]).strip()
        if tuple(value.casefold() for value in cells[:3]) == _HEADER:
            continue
        if not municipality or not _TIME.fullmatch(interval) or not streets:
            continue
        key = (normalize_text(municipality), normalize_text(interval))
        occurrence = occurrences[key]
        occurrences[key] += 1
        external_id = _notice_id(schedule_date, municipality, interval, occurrence)
        items.append(
            RawItem(
                source_id=source.id,
                external_id=external_id,
                url=public_url,
                title=f"{schedule_date} — {municipality}, {interval}",
                body=streets,
                published_at=None,
                canonical_url=_internal_canonical_url(public_url, external_id),
            )
        )
    return items


class EdsBelgradeOutagesCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "eds_belgrade_outages":
            raise ValueError(source.extraction.strategy)
        self._source = source
        self._client = client

    async def collect(self) -> list[RawItem]:
        response = await self._client.get(
            str(self._source.url),
            follow_redirects=True,
            timeout=25,
            headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
        )
        response.raise_for_status()
        return parse_eds_belgrade_outages(
            response.text,
            self._source,
            page_url=str(response.url),
        )
