"""Structured extraction for official Beogradske elektrane heating-network works."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.domain.dedup import normalize_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 2_000_000
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")

_PAGE_DATE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[^\s]+)\s+(?P<year>20\d{2})\s*[-–—]\s*"
    r"Radovi na toplovodnoj mreži$",
    re.IGNORECASE,
)
_POSTED_AT = re.compile(
    r"^Posted at\s+(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)h\b",
    re.IGNORECASE,
)
_AREA = re.compile(
    r"^Radovi na toplovodnoj mreži\s*[–—-]\s*(?P<area>\S.+)$",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "januar": 1,
    "јан": 1,
    "јануар": 1,
    "feb": 2,
    "februar": 2,
    "феб": 2,
    "фебруар": 2,
    "mar": 3,
    "mart": 3,
    "мар": 3,
    "март": 3,
    "apr": 4,
    "april": 4,
    "апр": 4,
    "април": 4,
    "maj": 5,
    "мај": 5,
    "jun": 6,
    "јун": 6,
    "jul": 7,
    "јул": 7,
    "avg": 8,
    "avgust": 8,
    "авг": 8,
    "август": 8,
    "sep": 9,
    "sept": 9,
    "septembar": 9,
    "сеп": 9,
    "септембар": 9,
    "okt": 10,
    "oktobar": 10,
    "окт": 10,
    "октобар": 10,
    "nov": 11,
    "novembar": 11,
    "нов": 11,
    "новембар": 11,
    "dec": 12,
    "decembar": 12,
    "дец": 12,
    "децембар": 12,
}


def _notice_id(publication_date: datetime, area: str) -> str:
    seed = f"{publication_date.date().isoformat()}\n{normalize_text(area)}".encode()
    return "beoelektrane-" + hashlib.sha256(seed).hexdigest()[:24]


def _internal_canonical_url(url: str, external_id: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("__beoelektrane_notice", external_id))
    query.sort()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _publication_time(lines: list[str], start: int) -> tuple[datetime, int]:
    first_area = len(lines)
    date_values: dict[tuple[int, int, int], int] = {}
    for index in range(start, len(lines)):
        if _AREA.match(lines[index]):
            first_area = index
            break
        match = _PAGE_DATE.match(lines[index])
        if match is None:
            continue
        month = _MONTHS.get(match.group("month").strip(".").casefold())
        if month is None:
            raise ValueError("unsupported Beogradske elektrane publication month")
        try:
            date_key = (int(match.group("year")), month, int(match.group("day")))
            datetime(*date_key)
        except ValueError as error:
            raise ValueError("invalid Beogradske elektrane publication date") from error
        date_values.setdefault(date_key, index)

    if not date_values:
        raise ValueError("Beogradske elektrane publication date missing")
    if len(date_values) != 1:
        raise ValueError("Beogradske elektrane publication date conflicting")

    (year, month, day), date_index = next(iter(date_values.items()))
    posted_values: set[tuple[int, int]] = set()
    for line in lines[date_index + 1 : first_area]:
        posted = _POSTED_AT.match(line)
        if posted is not None:
            posted_values.add((int(posted.group("hour")), int(posted.group("minute"))))
    if len(posted_values) > 1:
        raise ValueError("Beogradske elektrane publication time conflicting")
    hour, minute = next(iter(posted_values), (0, 0))
    return datetime(year, month, day, hour, minute, tzinfo=BELGRADE_TZ), first_area


def _is_footer(line: str) -> bool:
    folded = normalize_text(line)
    return folded.startswith("jkp beogradske elektrane") and "zahvaljuju" in folded


def _clean_body_line(line: str) -> str:
    return line.lstrip("-–—_• ").strip()


def parse_beoelektrane_network_works(
    html: str,
    source: SourceConfig,
    *,
    page_url: str | None = None,
) -> list[RawItem]:
    """Split the current official page into one stable item per municipality/district."""
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Beogradske elektrane works response too large")
    lines = normalize_visible_text(html).splitlines()
    try:
        start = lines.index("Radovi na toplovodnoj mreži") + 1
    except ValueError as error:
        raise ValueError("Beogradske elektrane works heading not found") from error

    published_at, first_area = _publication_time(lines, start)
    records: list[tuple[str, list[str]]] = []
    current_area: str | None = None
    current_body: list[str] = []
    footer_seen = False

    for line in lines[first_area:]:
        if _is_footer(line):
            footer_seen = True
            break
        area_match = _AREA.match(line)
        if area_match is not None:
            if current_area is not None:
                records.append((current_area, current_body))
            current_area = area_match.group("area").strip()
            current_body = []
            continue
        if current_area is None:
            if line:
                raise ValueError("unexpected content before Beogradske elektrane district heading")
            continue
        body_line = _clean_body_line(line)
        if body_line:
            current_body.append(body_line)

    if current_area is not None:
        records.append((current_area, current_body))
    if not footer_seen:
        raise ValueError("Beogradske elektrane works footer sentinel not found")
    if not records or len(records) > 25:
        raise ValueError("unexpected Beogradske elektrane district count")

    public_url = page_url or str(source.url)
    items: list[RawItem] = []
    seen_areas: set[str] = set()
    for area, body_lines in records:
        area_key = normalize_text(area)
        if area_key in seen_areas:
            raise ValueError("duplicate Beogradske elektrane district heading")
        seen_areas.add(area_key)
        body = "\n".join(body_lines).strip()
        if len(body) < 20:
            raise ValueError("Beogradske elektrane district lacks usable works details")
        external_id = _notice_id(published_at, area)
        title = f"Radovi na toplovodnoj mreži – {area}"
        items.append(
            RawItem(
                source_id=source.id,
                external_id=external_id,
                url=public_url,
                title=title,
                body=body,
                published_at=published_at,
                canonical_url=_internal_canonical_url(public_url, external_id),
            )
        )
    return items


def _allowed_url(value: str, source: SourceConfig) -> str:
    try:
        parts = urlsplit(value)
        source_parts = urlsplit(str(source.url))
        allowed_hosts = {
            source_parts.hostname,
            f"www.{source_parts.hostname}" if source_parts.hostname else None,
        }
        if (
            parts.scheme.lower() != "https"
            or parts.hostname not in allowed_hosts
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            raise ValueError
    except ValueError as error:
        raise ValueError("unapproved Beogradske elektrane URL") from error
    return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


class BeoelektraneNetworkWorksCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "beoelektrane_network_works":
            raise ValueError(source.extraction.strategy)
        self._source = source
        self._client = client
        _allowed_url(str(source.url), source)

    async def _get(self, value: str) -> httpx.Response:
        current = _allowed_url(value, self._source)
        for _ in range(6):
            response = await self._client.get(
                current,
                follow_redirects=False,
                timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
            )
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Beogradske elektrane redirect without location")
                current = _allowed_url(urljoin(current, location), self._source)
                continue
            response.raise_for_status()
            return response
        raise ValueError("Beogradske elektrane redirect limit")

    async def collect(self) -> list[RawItem]:
        response = await self._get(str(self._source.url))
        return parse_beoelektrane_network_works(
            response.text,
            self._source,
            page_url=str(response.url),
        )
