"""Evergreen official guidance: one undated page, bound to the hash of its visible text."""

from __future__ import annotations

import hashlib
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 2_000_000
MIN_BODY_CHARS = 800
MAX_REDIRECTS = 3
_CHROME = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")


def _approved(url: str, source: SourceConfig) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in source.extraction.approved_hosts:
        raise ValueError("guide host is not approved")
    return url


def parse_official_guide(html: str, source: SourceConfig) -> RawItem:
    if source.extraction.strategy != "official_guide" or source.policy.auto_publish:
        raise ValueError("official guides are review-only")
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(_CHROME):
        element.decompose()
    container = next(
        (found for selector in source.extraction.article_selectors
         if (found := soup.select_one(selector)) is not None),
        None,
    )
    if container is None:
        raise ValueError("guide content block not found")
    heading = container.find("h1") or soup.find("h1")
    title = (heading.get_text(" ", strip=True) if heading else "") or source.name
    body = " ".join(container.get_text(" ", strip=True).split())
    if len(body) < MIN_BODY_CHARS:
        raise ValueError("guide text too short to be the official page")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    url = str(source.url)
    return RawItem(
        source_id=source.id,
        external_id=f"guide-{source.id}-{digest[:24]}",
        url=url,
        title=title[:300],
        body=body,
        published_at=None,
        canonical_url=url,
        source_html=html,
    )


class OfficialGuideCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        self._source = source
        self._client = client
        _approved(str(source.url), source)

    async def _fetch(self) -> str:
        current = str(self._source.url)
        for _ in range(MAX_REDIRECTS + 1):
            _approved(current, self._source)
            response = await self._client.get(
                current, follow_redirects=False, timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
            )
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("guide redirect without location")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            if len(response.content) > MAX_BYTES:
                raise ValueError("guide response too large")
            return response.text
        raise ValueError("guide redirect limit")

    async def collect(self) -> list[RawItem]:
        return [parse_official_guide(await self._fetch(), self._source)]
