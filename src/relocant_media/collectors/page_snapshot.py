from hashlib import sha256

import httpx
from bs4 import BeautifulSoup

from relocant_media.collectors.base import RawItem
from relocant_media.sources.registry import SourceConfig


class UnsupportedExtractionStrategyError(ValueError):
    pass


def normalize_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["script", "style", "noscript", "template"]):
        element.decompose()

    lines = (" ".join(line.split()) for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


class PageSnapshotCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "page_change_detection":
            raise UnsupportedExtractionStrategyError(source.extraction.strategy)
        self._source = source
        self._client = client

    async def collect(self) -> list[RawItem]:
        response = await self._client.get(str(self._source.url), follow_redirects=True)
        response.raise_for_status()

        body = normalize_visible_text(response.text)
        if not body:
            return []

        content_hash = sha256(body.encode("utf-8")).hexdigest()
        soup = BeautifulSoup(response.text, "html.parser")
        title = (
            soup.title.get_text(" ", strip=True)
            if soup.title is not None
            else self._source.name
        )

        return [
            RawItem(
                source_id=self._source.id,
                external_id=content_hash,
                url=str(response.url),
                title=title,
                body=body,
                published_at=None,
                source_html=response.text,
            )
        ]
