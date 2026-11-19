"""Перечитать точный официальный анонс и его карту одним снимком."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.domain.event_programs import MARATHON_PAGE_URL, MARATHON_SOURCE_ID
from relocant_media.services.exhibition_map_point import MAX_HTML_BYTES
from relocant_media.services.marathon_event_map import (
    MARATHON_KML_URL,
    attach_marathon_kml,
)
from relocant_media.sources.registry import SourceConfig


def _publication_date(soup: BeautifulSoup) -> datetime:
    graph = soup.select_one('script.yoast-schema-graph[type="application/ld+json"]')
    if graph is None:
        raise ValueError("Метаданные программы отсутствуют")
    metadata = json.loads(graph.get_text())
    nodes = metadata.get("@graph") if isinstance(metadata, dict) else None
    if not isinstance(nodes, list):
        raise ValueError("Некорректные метаданные программы")
    pages = [node for node in nodes if isinstance(node, dict)
             and node.get("@type") == "WebPage" and node.get("@id") == MARATHON_PAGE_URL]
    published = pages[0].get("datePublished") if len(pages) == 1 else None
    if not isinstance(published, str):
        raise ValueError("Дата публикации программы отсутствует")
    stamp = datetime.fromisoformat(published)
    if stamp.tzinfo is None:
        raise ValueError("Дата публикации без часового пояса")
    return stamp.astimezone(UTC)


def parse_marathon_program(html: str, kml: str) -> RawItem:
    snapshot = attach_marathon_kml(html, kml)
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["header", "nav", "footer", "aside"]):
        element.decompose()
    title = next((node.get_text(" ", strip=True) for node in soup.find_all("h1")
                  if "7. Sava osiguranje Beogradski polumaraton" in node.get_text(" ", strip=True)),
                 None)
    lines = normalize_visible_text(str(soup.main or soup)).splitlines()
    start = next((i for i, line in enumerate(lines) if line.casefold() == "informacije"), None)
    if title is None or start is None:
        raise ValueError("Не найдена программа седьмого Белградского полумарафона")
    body = " ".join(lines[start:])
    if not 150 <= len(body) <= 18_000:
        raise ValueError("Некорректный размер официальной программы")
    return RawItem(
        source_id=MARATHON_SOURCE_ID, external_id=MARATHON_PAGE_URL,
        url=MARATHON_PAGE_URL, canonical_url=MARATHON_PAGE_URL,
        title=title, body=body, published_at=_publication_date(soup), source_html=snapshot,
    )


class BelgradeMarathonCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.id != MARATHON_SOURCE_ID:
            raise ValueError("Неизвестный реестр организатора полумарафона")
        self._client = client

    async def _fetch(self, url: str, *, max_bytes: int) -> str:
        async with self._client.stream("GET", url, follow_redirects=False) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise ValueError("Ответ организатора слишком большой")
                data.extend(chunk)
        return data.decode("utf-8")

    async def collect_url(self, url: str) -> RawItem:
        if url not in {MARATHON_PAGE_URL, MARATHON_PAGE_URL.rstrip("/")}:
            raise ValueError("Адрес программы полумарафона отсутствует в реестре")
        async with asyncio.timeout(36), asyncio.TaskGroup() as group:
            page = group.create_task(self._fetch(MARATHON_PAGE_URL, max_bytes=MAX_HTML_BYTES))
            map_data = group.create_task(self._fetch(MARATHON_KML_URL, max_bytes=300_000))
        return parse_marathon_program(page.result(), map_data.result())

    async def collect(self) -> list[RawItem]:
        return [await self.collect_url(MARATHON_PAGE_URL)]
