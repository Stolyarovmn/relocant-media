from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.page_snapshot import PageSnapshotCollector, normalize_visible_text
from relocant_media.sources.registry import load_source_file

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_PATH = Path("cities/belgrade/sources/euprava_foreigner_registration.yaml")


def test_normalize_visible_text_ignores_script_and_style() -> None:
    html = (FIXTURES / "euprava_foreigner_registration.html").read_text(encoding="utf-8")
    text = normalize_visible_text(html)
    assert "Prijava boravišta stranca" in text
    assert "fixtureNoise" not in text
    assert ".hidden" not in text


@pytest.mark.asyncio
async def test_page_snapshot_collector_is_deterministic() -> None:
    html = (FIXTURES / "euprava_foreigner_registration.html").read_text(encoding="utf-8")
    configured = load_source_file(SOURCE_PATH)
    source = configured.model_copy(
        update={
            "extraction": configured.extraction.model_copy(
                update={"strategy": "page_change_detection"}
            )
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == str(source.url)
        return httpx.Response(200, text=html, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = PageSnapshotCollector(source, client)
        first = await collector.collect()
        second = await collector.collect()

    assert len(first) == 1
    assert first == second
    assert first[0].source_id == "euprava_foreigner_registration"
    assert first[0].published_at is None
    assert first[0].source_html == html
    assert first[0].approved_image_hosts == ()
