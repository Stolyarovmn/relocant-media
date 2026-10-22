"""Structured evidence from the official BitefArtCafe concert programme."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 3_000_000
MAX_EVENTS = 30
MAX_ITEM_ERROR_MESSAGE = 200
_LIST_PATH = "/program/koncerti"
_DETAIL_PATH = re.compile(r"^/dogadjaji/(?P<slug>[a-z0-9][a-z0-9-]*)/?$")
_DATE_SEARCH = re.compile(
    r"(?<!\d)(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.20\d{2}\.(?!\d)"
)
_TITLE_SUFFIX = " - BitefArtCafe"
_BODY_STOP = "Online Rezervacije"


def _item_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ITEM_ERROR_MESSAGE]


def _allowed_url(value: str, source: SourceConfig, *, detail: bool) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved BitefArtCafe URL") from error

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
        raise ValueError("unapproved BitefArtCafe URL")
    try:
        ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        pass
    else:
        raise ValueError("IP BitefArtCafe host forbidden")
    if parsed.hostname in {"localhost", "metadata.google.internal"}:
        raise ValueError("private BitefArtCafe host forbidden")

    path = parsed.path.rstrip("/") or "/"
    if detail:
        if _DETAIL_PATH.fullmatch(path) is None:
            raise ValueError("unapproved BitefArtCafe detail path")
    elif path != _LIST_PATH:
        raise ValueError("unapproved BitefArtCafe list path")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", "", ""))


def _concert_links(html: str, source: SourceConfig) -> list[str]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("BitefArtCafe concert list response too large")
    if "Koncerti" not in normalize_visible_text(html).splitlines():
        raise ValueError("BitefArtCafe concert heading missing")

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
        if len(links) > MAX_EVENTS:
            raise ValueError("unexpected BitefArtCafe concert count")
    if not links:
        raise ValueError("BitefArtCafe concert detail links missing")
    return links


def _detail_headings(soup: BeautifulSoup) -> tuple[list[Tag], str]:
    if soup.title is None:
        raise ValueError("BitefArtCafe page title missing")
    page_title = soup.title.get_text(" ", strip=True)
    if not page_title.endswith(_TITLE_SUFFIX):
        raise ValueError("BitefArtCafe page title suffix missing")
    title = page_title[: -len(_TITLE_SUFFIX)].strip()
    if not title:
        raise ValueError("BitefArtCafe event title missing")

    headings = [
        node
        for node in soup.find_all(["h1", "h2"])
        if isinstance(node, Tag) and node.get_text(" ", strip=True) == title
    ]
    if not headings:
        raise ValueError("BitefArtCafe event heading does not match page title")
    return headings, title


def _body_after_heading(title_node: Tag, title: str) -> tuple[list[str], bool]:
    lines: list[str] = []
    for element in title_node.find_all_next():
        if not isinstance(element, Tag):
            continue
        text = element.get_text(" ", strip=True)
        if element.name in {"h1", "h2", "h3"} and text == _BODY_STOP:
            return lines, True
        if element.name in {"script", "style", "noscript", "template"}:
            continue
        if element.name in {"h1", "h2", "h3"} and text == title:
            continue
        if element.name in {"p", "li", "h2", "h3"} and text:
            if not lines or lines[-1] != text:
                lines.append(text)
    return lines, False


def _detail_body(html: str) -> tuple[str, str]:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("BitefArtCafe event response too large")
    visible = normalize_visible_text(html)
    dates = _DATE_SEARCH.findall(visible)
    if not dates:
        raise ValueError("BitefArtCafe event date missing")
    event_date = dates[0]

    soup = BeautifulSoup(html, "html.parser")
    title_nodes, title = _detail_headings(soup)
    candidates: list[str] = []
    boundary_found = False
    for title_node in title_nodes:
        lines, found = _body_after_heading(title_node, title)
        boundary_found = boundary_found or found
        if found and lines:
            candidates.append("\n".join(lines))
    if not boundary_found:
        raise ValueError("BitefArtCafe event body boundary missing")
    if not candidates:
        raise ValueError("BitefArtCafe event body missing")

    body = max(candidates, key=len)
    if len(body) < 120:
        raise ValueError("BitefArtCafe event body shorter than 120 characters")
    if event_date not in body:
        body = f"Datum događaja: {event_date}\n{body}"
    return title, body


def parse_bitefartcafe_event(html: str, url: str, source: SourceConfig) -> RawItem:
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


class BitefArtCafeEventsCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "bitefartcafe_events":
            raise ValueError(source.extraction.strategy)
        if source.policy.auto_publish:
            raise ValueError("BitefArtCafe events must stay review-only without publication dates")
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
                        raise ValueError("BitefArtCafe redirect without location")
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
                        raise ValueError("BitefArtCafe response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("BitefArtCafe response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("BitefArtCafe redirect limit")

    async def collect(self) -> list[RawItem]:
        self.item_failures = []
        listing = await self._fetch(str(self._source.url), detail=False)
        links = _concert_links(listing, self._source)
        items: list[RawItem] = []
        for link in links:
            try:
                items.append(
                    parse_bitefartcafe_event(
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
                "all BitefArtCafe event details failed extraction: "
                f"{first['error_type']}: {first['error_message']}"
            )
        return items
