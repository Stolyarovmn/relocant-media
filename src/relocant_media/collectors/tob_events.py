"""Structured evidence from the official Tourist Organization of Belgrade events pages."""

from __future__ import annotations

import ipaddress
import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 3_000_000
MAX_EVENTS = 40
MAX_ITEM_ERROR_MESSAGE = 200
_DATE = re.compile(
    r"^(?P<day>\d{2})\.\s*(?P<month>\d{2})\.\s*(?P<year>20\d{2})\.$"
)
_DETAIL_PATH = re.compile(r"^/sr/events/(?P<slug>[a-z0-9][a-z0-9-]*)/?$")
_LIST_PATH = "/sr/events"
_REQUIRED_LABELS = ("Datum početka:", "Datum završetka:", "Mesto održavanja:")
_INFO_SUFFIX = " | Informacije"


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_url(value: str, source: SourceConfig, *, detail: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved TOB URL") from error

    source_url = urlsplit(str(source.url))
    allowed_hosts = {source_url.hostname, *source.extraction.approved_hosts}
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError("unapproved TOB URL")
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("IP TOB host forbidden")
    if parsed.hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("private TOB host forbidden")

    path = parsed.path.rstrip("/") or "/"
    if detail:
        if _DETAIL_PATH.fullmatch(path) is None:
            raise ValueError("unapproved TOB detail path")
    elif path != _LIST_PATH:
        raise ValueError("unapproved TOB list path")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))


def _event_links(html: str, source: SourceConfig) -> list[str]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("TOB events response too large")
    soup = BeautifulSoup(html, "html.parser")
    heading = next(
        (
            element
            for element in soup.find_all(["h1", "h2"])
            if element.get_text(" ", strip=True) == "Događaji"
        ),
        None,
    )
    if heading is None:
        raise ValueError("TOB events heading missing")

    links: list[str] = []
    seen: set[str] = set()
    for element in heading.find_all_next():
        if not isinstance(element, Tag):
            continue
        if (
            element.name in {"h1", "h2"}
            and element.get_text(" ", strip=True) == "Ne propustite"
        ):
            break
        if element.name != "a":
            continue
        href = element.get("href")
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
        if len(links) > MAX_EVENTS:
            raise ValueError("unexpected TOB event count")
    if not links:
        raise ValueError("TOB event detail links missing")
    return links


def _parse_date(value: str, *, label: str) -> date:
    match = _DATE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"invalid TOB {label} date")
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as error:
        raise ValueError(f"invalid TOB {label} date") from error


def _event_heading(soup: BeautifulSoup) -> tuple[Tag, str]:
    matches: list[tuple[Tag, str]] = []
    for info_heading in soup.find_all("h2"):
        info_text = info_heading.get_text(" ", strip=True)
        if not info_text.endswith(_INFO_SUFFIX):
            continue
        title = info_text[: -len(_INFO_SUFFIX)].strip()
        previous_h1 = info_heading.find_previous("h1")
        if (
            isinstance(previous_h1, Tag)
            and title
            and previous_h1.get_text(" ", strip=True) == title
        ):
            matches.append((previous_h1, title))
    if len(matches) != 1:
        raise ValueError("TOB event title/information heading pair missing or ambiguous")
    return matches[0]


def _detail_body(html: str) -> tuple[str, str, date, date]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("TOB event response too large")
    soup = BeautifulSoup(html, "html.parser")
    title_node, title = _event_heading(soup)

    lines: list[str] = []
    for element in title_node.find_all_next():
        if not isinstance(element, Tag):
            continue
        text = element.get_text(" ", strip=True)
        if element.name in {"h1", "h2"} and text == "Ne propustite":
            break
        if element.name in {"script", "style", "noscript", "template"}:
            continue
        if element.name in {"p", "li", "h2", "h3"} and text:
            if not lines or lines[-1] != text:
                lines.append(text)

    if not lines:
        raise ValueError("TOB event body missing")
    joined = "\n".join(lines)
    values: dict[str, str] = {}
    for label in _REQUIRED_LABELS:
        matching = [line for line in lines if line.startswith(label)]
        if len(matching) != 1:
            raise ValueError(f"TOB event requires exactly one {label.rstrip(':')}")
        values[label] = matching[0][len(label) :].strip()
        if not values[label]:
            raise ValueError(f"TOB event {label.rstrip(':')} missing")

    start = _parse_date(values["Datum početka:"], label="start")
    end = _parse_date(values["Datum završetka:"], label="end")
    if end < start:
        raise ValueError("TOB event end precedes start")

    visible_text = BeautifulSoup(html, "html.parser").get_text("\n")
    visible = [line for line in visible_text.splitlines() if line.strip()]
    normalized_visible = [" ".join(line.split()) for line in visible]
    title_indexes = [index for index, line in enumerate(normalized_visible) if line == title]
    if len(title_indexes) != 1:
        raise ValueError("TOB event title occurrence missing or ambiguous")
    title_index = title_indexes[0]
    leading_dates = [
        _parse_date(line, label="displayed")
        for line in normalized_visible[title_index + 1 : title_index + 5]
        if _DATE.fullmatch(line)
    ]
    if len(leading_dates) != 1 or leading_dates[0] != start:
        raise ValueError("TOB displayed event date does not match start date")

    if len(joined) < 120:
        raise ValueError("TOB event body shorter than 120 characters")
    return title, joined, start, end


def parse_tob_event(html: str, url: str, source: SourceConfig) -> RawItem:
    canonical = _allowed_url(url, source, detail=True)
    title, body, _start, _end = _detail_body(html)
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


class TobEventsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "tob_events":
            raise ValueError(source.extraction.strategy)
        if source.policy.auto_publish:
            raise ValueError("TOB events must stay review-only without publication dates")
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
                        raise ValueError("TOB redirect without location")
                    current = _allowed_url(
                        urljoin(current, location),
                        self._source,
                        detail=detail,
                    )
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > MAX_BYTES:
                        raise ValueError("TOB response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("TOB response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("TOB redirect limit")

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        listing = await self._fetch(str(self._source.url), detail=False)
        links = _event_links(listing, self._source)
        items: list[RawItem] = []
        for link in links:
            try:
                items.append(
                    parse_tob_event(
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
                "all TOB event details failed extraction: "
                f"{first['error_type']}: {first['error_message']}"
            )
        return items
