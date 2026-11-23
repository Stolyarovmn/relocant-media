"""Structured extraction for the official BVK planned-water-works page."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from relocant_media.collectors.base import ArticleBatchError, RawItem, article_failure
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.domain.dedup import normalize_text
from relocant_media.domain.water_bulletin import is_shared_tanker_heading
from relocant_media.sources.registry import SourceConfig

_ENTRY = re.compile(r"^(?P<date>\d{2}\.\d{2}\.\d{4}\.)\s+(?P<title>\S.+)$")
_STOP_LINES = {"Инфо", "Informacije", "Информације"}
_SKIP_LINES = {"Списак општина и улица", "Погледајте списак цистерни"}
MAX_BYTES = 2_000_000
_MAX_FAILURES = 20


def _entry_id(date_text: str, title: str) -> str:
    seed = f"{date_text}\n{normalize_text(title)}".encode()
    return "bvk-" + hashlib.sha256(seed).hexdigest()[:24]


def _internal_canonical_url(url: str, external_id: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("__bvk_notice", external_id))
    query.sort()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def parse_bvk_planned_works(
    html: str,
    source: SourceConfig,
    *,
    page_url: str | None = None,
    item_failures: list[dict[str, str]] | None = None,
) -> list[RawItem]:
    """Split the live BVK list into independent notices without inventing publication dates."""
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("BVK planned-works response too large")
    lines = normalize_visible_text(html).splitlines()
    try:
        start = lines.index("Планирани радови") + 1
    except ValueError as error:
        raise ValueError("BVK planned-works heading not found") from error

    records: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None
    for line in lines[start:]:
        if line in _STOP_LINES:
            break
        if line in _SKIP_LINES:
            continue
        if is_shared_tanker_heading(line):
            if current is not None:
                records.append(current)
            current = None
            continue
        match = _ENTRY.match(line)
        if match:
            if current is not None:
                records.append(current)
            current = (match.group("date"), match.group("title").strip(), [])
            continue
        if current is not None:
            current[2].append(line)
    if current is not None:
        records.append(current)
    if not records:
        raise ValueError("BVK planned-works entries not found")

    public_url = page_url or str(source.url)
    items: list[RawItem] = []
    failures: list[dict[str, str]] = []
    seen: set[str] = set()
    for record_number, (date_text, title, body_lines) in enumerate(records, 1):
        body = "\n".join(line for line in body_lines if line).strip()
        if len(body) < 40:
            if len(failures) < _MAX_FAILURES:
                failures.append(article_failure(
                    public_url,
                    ValueError(f"BVK planned-works entry {record_number} lacks a usable body"),
                ))
            continue
        external_id = _entry_id(date_text, title)
        if external_id in seen:
            continue
        seen.add(external_id)
        items.append(
            RawItem(
                source_id=source.id,
                external_id=external_id,
                url=public_url,
                title=f"{date_text} {title}",
                body=body,
                published_at=None,
                canonical_url=_internal_canonical_url(public_url, external_id),
            )
        )
    if not items:
        raise ArticleBatchError("BVK planned-works entries lack usable bodies", failures)
    if item_failures is not None:
        item_failures.extend(failures)
    return items


class BvkPlannedWorksCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "bvk_planned_works":
            raise ValueError(source.extraction.strategy)
        self._source = source
        self._client = client
        self.item_failures: list[dict[str, str]] = []

    async def collect(self) -> list[RawItem]:
        self.item_failures.clear()
        response = await self._client.get(
            str(self._source.url),
            follow_redirects=True,
            timeout=25,
            headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
        )
        response.raise_for_status()
        return parse_bvk_planned_works(
            response.text,
            self._source,
            page_url=str(response.url),
            item_failures=self.item_failures,
        )
