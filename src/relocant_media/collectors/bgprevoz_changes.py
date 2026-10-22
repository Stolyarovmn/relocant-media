"""Structured extraction for official Belgrade public-transport service changes."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 2_000_000
MAX_NOTICES = 50
MAX_ITEM_ERROR_MESSAGE = 200

_NOTICE_PATH = re.compile(
    r"^(?:/index\.php)?/linije/aktuelne-izmene/(?P<notice_id>\d+)(?:/|$)",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"^(?P<start>\d{2}/\d{2}/20\d{2})\s*[-–—]\s*(?P<end>\d{2}/\d{2}/20\d{2})$"
)
_STOP_LINES = {
    "Зона радова",
    "Sekretarijat za javni prevoz",
    "Секретаријат за јавни превоз",
    "Gradska uprava grada Beograda",
    "Градска управа града Београда",
}


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_hostnames(source: SourceConfig) -> set[str]:
    hostname = urlsplit(str(source.url)).hostname
    if not hostname:
        raise ValueError("Bgprevoz source hostname missing")
    if hostname.startswith("www."):
        return {hostname, hostname.removeprefix("www.")}
    return {hostname, f"www.{hostname}"}


def _allowed_url(value: str, source: SourceConfig) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise ValueError("unapproved Bgprevoz URL") from error
    if (
        parts.scheme.lower() != "https"
        or parts.hostname not in _allowed_hostnames(source)
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
    ):
        raise ValueError("unapproved Bgprevoz URL")
    return urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))


def _notice_identity(value: str, source: SourceConfig) -> tuple[str, str]:
    allowed = _allowed_url(value, source)
    parts = urlsplit(allowed)
    match = _NOTICE_PATH.match(parts.path)
    if match is None:
        raise ValueError("Bgprevoz notice URL has unexpected path")
    notice_id = match.group("notice_id")
    source_parts = urlsplit(str(source.url))
    canonical = urlunsplit(
        (
            "https",
            source_parts.netloc,
            f"/linije/aktuelne-izmene/{notice_id}",
            "",
            "",
        )
    )
    return notice_id, canonical


def parse_bgprevoz_change_links(html: str, source: SourceConfig) -> tuple[str, ...]:
    """Extract unique official notice URLs from the current-changes list."""
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Bgprevoz changes list response too large")
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        absolute = urljoin(str(source.url), href)
        try:
            notice_id, canonical = _notice_identity(absolute, source)
        except ValueError:
            continue
        if notice_id in seen:
            continue
        seen.add(notice_id)
        result.append(canonical)
        if len(result) > MAX_NOTICES:
            raise ValueError("unexpected Bgprevoz notice count")
    if not result:
        raise ValueError("Bgprevoz current-change notice links not found")
    return tuple(result)


def _parse_period(value: str) -> tuple[datetime, datetime]:
    match = _PERIOD.match(value)
    if match is None:
        raise ValueError("Bgprevoz change period missing")
    try:
        start = datetime.strptime(match.group("start"), "%d/%m/%Y")
        end = datetime.strptime(match.group("end"), "%d/%m/%Y")
    except ValueError as error:
        raise ValueError("Bgprevoz change period invalid") from error
    if end < start:
        raise ValueError("Bgprevoz change period invalid")
    return start, end


def parse_bgprevoz_change(
    html: str,
    source: SourceConfig,
    *,
    page_url: str,
) -> RawItem:
    """Parse a detail page without treating its effective period as publication time."""
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Bgprevoz change response too large")
    notice_id, canonical_url = _notice_identity(page_url, source)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading is not None else ""
    if not title:
        raise ValueError("Bgprevoz change title missing")

    lines = normalize_visible_text(html).splitlines()
    try:
        title_index = lines.index(title)
    except ValueError as error:
        raise ValueError("Bgprevoz change title missing from visible page") from error

    period_index: int | None = None
    period_line = ""
    for index in range(title_index + 1, min(len(lines), title_index + 40)):
        if _PERIOD.match(lines[index]):
            period_index = index
            period_line = lines[index]
            break
    if period_index is None:
        raise ValueError("Bgprevoz change period missing")
    start, end = _parse_period(period_line)

    body_lines: list[str] = []
    for line in lines[period_index + 1 :]:
        if line in _STOP_LINES:
            break
        body_lines.append(line)
    body_text = "\n".join(body_lines).strip()
    if len(body_text) < 120:
        raise ValueError("Bgprevoz change body shorter than 120 characters")

    period_text = f"Период измене: {start:%d.%m.%Y}–{end:%d.%m.%Y}."
    body = f"{period_text}\n{body_text}"
    return RawItem(
        source_id=source.id,
        external_id=f"bgprevoz-{notice_id}",
        url=canonical_url,
        title=title,
        body=body,
        published_at=None,
        canonical_url=canonical_url,
        source_html=html,
    )


class BgprevozChangesCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "bgprevoz_changes":
            raise ValueError(source.extraction.strategy)
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        _allowed_url(str(source.url), source)

    async def _fetch(self, value: str) -> tuple[str, str]:
        current = _allowed_url(value, self._source)
        for _ in range(6):
            async with self._client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Bgprevoz redirect without location")
                    current = _allowed_url(urljoin(current, location), self._source)
                    continue
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Bgprevoz response too large")
                return data.decode(response.encoding or "utf-8"), str(response.url)
        raise ValueError("Bgprevoz redirect limit")

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        list_html, _ = await self._fetch(str(self._source.url))
        notice_urls = parse_bgprevoz_change_links(list_html, self._source)
        items: list[RawItem] = []
        for notice_url in notice_urls:
            try:
                html, final_url = await self._fetch(notice_url)
                items.append(
                    parse_bgprevoz_change(html, self._source, page_url=final_url)
                )
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": notice_url,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            raise ValueError("all Bgprevoz current-change notices failed extraction")
        return items

    async def collect_url(self, url: str) -> RawItem:
        _, canonical = _notice_identity(url, self._source)
        html, final_url = await self._fetch(canonical)
        return parse_bgprevoz_change(html, self._source, page_url=final_url)
