"""Structured evidence from the official Museum of Yugoslavia program calendar."""

from __future__ import annotations

import ipaddress
import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 3_000_000
MAX_PROGRAMS = 30
MAX_ITEM_ERROR_MESSAGE = 200
_LIST_PATH = "/rs/kalendar"
_DETAIL_PATH = re.compile(r"^/rs/program/(?P<slug>[a-z0-9][a-z0-9-]*)/?$")
_CALENDAR_HEADING = "ИЗЛОЖБЕ И ПРОГРАМИ"
_DAY = re.compile(
    r"^Дан:\s*(?P<start_day>\d{2})\.(?P<start_month>\d{2})-"
    r"(?P<end_day>\d{2})\.(?P<end_month>\d{2})\.(?P<year>20\d{2})\.$"
)
_TIME = re.compile(r"^Време:\s*(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_url(value: str, source: SourceConfig, *, detail: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved Museum of Yugoslavia URL") from error

    source_url = urlsplit(str(source.url))
    allowed_hosts = {source_url.hostname, *source.extraction.approved_hosts}
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("unapproved Museum of Yugoslavia URL")
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("IP Museum of Yugoslavia host forbidden")
    if parsed.hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("private Museum of Yugoslavia host forbidden")

    path = parsed.path.rstrip("/") or "/"
    if detail:
        if _DETAIL_PATH.fullmatch(path) is None:
            raise ValueError("unapproved Museum of Yugoslavia detail path")
    elif path != _LIST_PATH:
        raise ValueError("unapproved Museum of Yugoslavia calendar path")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))


def _program_links(html: str, source: SourceConfig) -> list[str]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Museum of Yugoslavia calendar response too large")
    visible_lines = normalize_visible_text(html).splitlines()
    if _CALENDAR_HEADING not in visible_lines:
        raise ValueError("Museum of Yugoslavia calendar heading missing")
    soup = BeautifulSoup(html, "html.parser")

    links: list[str] = []
    seen: set[str] = set()
    for node in soup.find_all("a"):
        if not isinstance(node, Tag):
            continue
        href = node.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        candidate = urljoin(str(source.url), href.strip())
        try:
            candidate = _allowed_url(candidate, source, detail=True)
        except ValueError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        links.append(candidate)
        if len(links) > MAX_PROGRAMS:
            raise ValueError("unexpected Museum of Yugoslavia program count")
    if not links:
        raise ValueError("Museum of Yugoslavia program links missing")
    return links


def _parse_day(value: str) -> tuple[date, date]:
    match = _DAY.fullmatch(value.strip())
    if match is None:
        raise ValueError("invalid Museum of Yugoslavia program day range")
    year = int(match.group("year"))
    try:
        start = date(
            year,
            int(match.group("start_month")),
            int(match.group("start_day")),
        )
        end = date(
            year,
            int(match.group("end_month")),
            int(match.group("end_day")),
        )
    except ValueError as error:
        raise ValueError("invalid Museum of Yugoslavia program day range") from error
    if end < start:
        raise ValueError("Museum of Yugoslavia program end precedes start")
    return start, end


def _detail_body(html: str) -> tuple[str, str]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Museum of Yugoslavia program response too large")
    soup = BeautifulSoup(html, "html.parser")
    titles = [node for node in soup.find_all("h1") if node.get_text(" ", strip=True)]
    if len(titles) != 1 or not isinstance(titles[0], Tag):
        raise ValueError("Museum of Yugoslavia program title missing or ambiguous")
    title_node = titles[0]
    title = title_node.get_text(" ", strip=True)

    day_nodes = [
        node
        for node in soup.find_all("li")
        if isinstance(node, Tag) and node.get_text(" ", strip=True).startswith("Дан:")
    ]
    if len(day_nodes) != 1:
        raise ValueError("Museum of Yugoslavia program requires exactly one day range")
    day_node = day_nodes[0]
    day_text = day_node.get_text(" ", strip=True)
    _parse_day(day_text)

    time_nodes = [
        node
        for node in soup.find_all("li")
        if isinstance(node, Tag) and node.get_text(" ", strip=True).startswith("Време:")
    ]
    if len(time_nodes) > 1:
        raise ValueError("Museum of Yugoslavia program time is ambiguous")
    time_text: str | None = None
    if time_nodes:
        time_text = time_nodes[0].get_text(" ", strip=True)
        if _TIME.fullmatch(time_text) is None:
            raise ValueError("invalid Museum of Yugoslavia program time")

    location_node = day_node.find_previous("a")
    if not isinstance(location_node, Tag):
        raise ValueError("Museum of Yugoslavia program location missing")
    location = location_node.get_text(" ", strip=True)
    if not location or location == "Листа програма":
        raise ValueError("Museum of Yugoslavia program location missing")

    paragraphs: list[str] = []
    for node in title_node.find_all_next():
        if node is day_node:
            break
        if not isinstance(node, Tag) or node.name != "p":
            continue
        text = node.get_text(" ", strip=True)
        if text and (not paragraphs or paragraphs[-1] != text):
            paragraphs.append(text)
    paragraph_text = "\n".join(paragraphs).strip()
    if len(paragraph_text) < 120:
        raise ValueError("Museum of Yugoslavia program body shorter than 120 characters")

    lines = [*paragraphs, f"Место: {location}", day_text]
    if time_text is not None:
        lines.append(time_text)
    return title, "\n".join(lines)


def parse_yugoslavia_program(html: str, url: str, source: SourceConfig) -> RawItem:
    canonical = _allowed_url(url, source, detail=True)
    title, body = _detail_body(html)
    return RawItem(
        source_id=source.id,
        external_id=canonical,
        url=canonical,
        title=title,
        body=body,
        published_at=None,
        canonical_url=canonical,
        source_html=html,
    )


class YugoslaviaProgramsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "yugoslavia_programs":
            raise ValueError(source.extraction.strategy)
        if source.policy.auto_publish:
            raise ValueError(
                "Museum of Yugoslavia programs must stay review-only without publication dates"
            )
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []
        _allowed_url(str(source.url), source, detail=False)

    async def _fetch(self, value: str, *, detail: bool) -> str:
        current = _allowed_url(value, self._source, detail=detail)
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
                        raise ValueError("Museum of Yugoslavia redirect without location")
                    current = _allowed_url(
                        urljoin(current, location),
                        self._source,
                        detail=detail,
                    )
                    continue
                if response.status_code == 525:
                    raise httpx.ConnectError(
                        "Museum of Yugoslavia upstream TLS handshake failed",
                        request=response.request,
                    )
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > MAX_BYTES:
                        raise ValueError("Museum of Yugoslavia response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Museum of Yugoslavia response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("Museum of Yugoslavia redirect limit")

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        listing = await self._fetch(str(self._source.url), detail=False)
        links = _program_links(listing, self._source)
        items: list[RawItem] = []
        for link in links:
            try:
                items.append(
                    parse_yugoslavia_program(
                        await self._fetch(link, detail=True),
                        link,
                        self._source,
                    )
                )
            except (ValueError, httpx.HTTPError) as error:
                self.item_failures.append(
                    {
                        "url": link,
                        "error_type": type(error).__name__,
                        "error_message": _item_error_message(error),
                    }
                )
        if self.item_failures and not items:
            first = self.item_failures[0]
            raise ValueError(
                "all Museum of Yugoslavia program details failed extraction: "
                f"{first['error_type']}: {first['error_message']}"
            )
        return items
