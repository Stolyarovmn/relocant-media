"""Structured extraction for official EDS Belgrade planned-outage day pages."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime
from itertools import chain
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from bs4.element import Tag

from relocant_media.collectors.base import ArticleBatchError, RawItem, article_failure
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
_MAX_FAILURES = 20


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
        for cell in row.find_all(["th", "td"], recursive=False)
    ]


def _is_header(cells: list[str]) -> bool:
    return tuple(value.casefold() for value in cells[:3]) == _HEADER


def parse_eds_belgrade_outages(
    html: str,
    source: SourceConfig,
    *,
    page_url: str | None = None,
    item_failures: list[dict[str, str]] | None = None,
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
    tables = [
        [_cells(row) for row in table.find_all("tr") if row.find_parent("table") is table]
        for table in soup.find_all("table")
    ]
    if not any(tables):
        raise ValueError("EDS outage table not found")

    schedules = [rows for rows in tables if any(_is_header(cells) for cells in rows)]
    if not schedules:
        raise ValueError("EDS outage table header not found")

    public_url = page_url or str(source.url)
    occurrences: defaultdict[tuple[str, str], int] = defaultdict(int)
    items: list[RawItem] = []
    failures: list[dict[str, str]] = []
    for row_number, cells in enumerate(chain.from_iterable(schedules), 1):
        if _is_header(cells) or not any(cells):
            continue
        municipality = cells[0]
        interval = cells[1] if len(cells) > 1 else ""
        streets = " ".join(cells[2:]).strip()
        # A broken address cell must not shift the identity of the next row with the same hours.
        valid_identity = bool(municipality and _TIME.fullmatch(interval))
        occurrence = 0
        if valid_identity:
            key = (normalize_text(municipality), normalize_text(interval))
            occurrence = occurrences[key]
            occurrences[key] += 1
        if not valid_identity or not streets:
            if len(failures) < _MAX_FAILURES:
                failures.append(article_failure(
                    public_url, ValueError(f"invalid EDS schedule row {row_number}"),
                ))
            continue
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
    if failures and not items:
        raise ArticleBatchError("all EDS schedule rows failed extraction", failures)
    if item_failures is not None:
        item_failures.extend(failures)
    return items


class EdsBelgradeOutagesCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "eds_belgrade_outages":
            raise ValueError(source.extraction.strategy)
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        self.empty_reason: str | None = None

    async def collect(self) -> list[RawItem]:
        self.item_failures.clear()
        self.empty_reason = None
        response = await self._client.get(
            str(self._source.url),
            follow_redirects=True,
            timeout=25,
            headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
        )
        response.raise_for_status()
        items = parse_eds_belgrade_outages(
            response.text,
            self._source,
            page_url=str(response.url),
            item_failures=self.item_failures,
        )
        if not items:
            self.empty_reason = "empty_index"
        return items
