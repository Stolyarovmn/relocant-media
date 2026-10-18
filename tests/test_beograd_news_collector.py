from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors.beograd_news import (
    BelgradeNewsListCollector,
    parse_beograd_news_list,
)
from relocant_media.sources.registry import load_source_file

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_PATH = Path("cities/belgrade/sources/city_beograd_news.yaml")


def test_parse_beograd_news_list_extracts_stable_ids_and_dates() -> None:
    html = (FIXTURES / "beograd_news_list.html").read_text(encoding="utf-8")
    source = load_source_file(SOURCE_PATH)

    items = parse_beograd_news_list(html, source)

    assert [item.external_id for item in items] == ["116232", "116264"]
    assert [item.title for item in items] == [
        "Bez vode sutra delovi Zvezdare",
        "Održana uspešna promocija Beograda u Pekingu",
    ]
    assert items[0].url == (
        "https://www.beograd.rs/lat/beoinfo-vesti/a116232/Bez-vode-sutra-delovi-Zvezdare.html"
    )
    assert items[0].published_at == datetime(2026, 10, 15, tzinfo=ZoneInfo("Europe/Belgrade"))


@pytest.mark.asyncio
async def test_beograd_news_collector_fetches_list() -> None:
    html = (FIXTURES / "beograd_news_list.html").read_text(encoding="utf-8")
    source = load_source_file(SOURCE_PATH)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == str(source.url)
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await BelgradeNewsListCollector(source, client).collect()

    assert len(items) == 2
    assert {item.external_id for item in items} == {"116232", "116264"}
