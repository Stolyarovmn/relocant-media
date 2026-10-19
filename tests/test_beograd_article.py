from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors.beograd_article import (
    ArticleExtractionError,
    BelgradeArticleCollector,
    parse_beograd_article,
)
from relocant_media.sources.registry import load_source_file

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_PATH = Path("cities/belgrade/sources/city_beograd_news.yaml")
ARTICLE_URL = (
    "https://www.beograd.rs/lat/beoinfo-vesti/"
    "a116232/Bez-vode-sutra-delovi-Zvezdare.html"
)


def test_parse_beograd_article_extracts_normalized_evidence() -> None:
    html = (FIXTURES / "beograd_article.html").read_text(encoding="utf-8")
    source = load_source_file(SOURCE_PATH)

    item = parse_beograd_article(html, source, ARTICLE_URL)

    assert item.external_id == "116232"
    assert item.title == "Bez vode sutra delovi Zvezdare"
    assert item.body == (
        "Prvi pasus probnog sadržaja vesti.\n\n"
        "Drugi pasus sa dodatnim servisnim informacijama."
    )
    assert "Navigation" not in item.body
    assert "Footer" not in item.body
    assert item.published_at == datetime(2026, 10, 15, tzinfo=ZoneInfo("Europe/Belgrade"))


def test_parse_beograd_article_fails_closed_without_body() -> None:
    source = load_source_file(SOURCE_PATH)
    html = "<main><h1>Naslov</h1><time>15.10.2026.</time></main>"

    with pytest.raises(ArticleExtractionError, match="body paragraphs"):
        parse_beograd_article(html, source, ARTICLE_URL)


def test_parse_nuxt_article_preserves_content_without_footer_or_signup() -> None:
    html = (FIXTURES / "beograd_article_nuxt.html").read_text(encoding="utf-8")

    item = parse_beograd_article(html, load_source_file(SOURCE_PATH), ARTICLE_URL)

    assert item.body == (
        "Najavljeni su novi projekti.\n\n"
        "Prvi pasus. Drugi deo istog pasusa.\n\n"
        "Grad Beograd govori o autorskim pravima © i prijavi na obaveštenja.\n\n"
        "Poslednji sadržajni pasus sa svim detaljima."
    )
    assert item.published_at == datetime(2026, 10, 18, tzinfo=ZoneInfo("Europe/Belgrade"))


@pytest.mark.parametrize("container", ["article", "main", "div"])
def test_generic_article_excludes_semantic_chrome_on_same_line(container: str) -> None:
    html = (
        f"<{container}><p>Before heading.</p><h1>Naslov</h1>"
        "<p>Full article.</p><footer><p>Copyright.</p>"
        "<form><p>Signup success.</p></form></footer>"
        f"<p>Last article paragraph.</p></{container}>"
    )

    item = parse_beograd_article(html, load_source_file(SOURCE_PATH), ARTICLE_URL)

    assert item.body == "Full article.\n\nLast article paragraph."


def test_nuxt_empty_body_does_not_fall_back_to_footer() -> None:
    html = (
        '<h1>Naslov</h1><div document-title="Naslov"><div class="story__rte"></div></div>'
        "<footer><p>Copyright.</p><p>Signup success.</p></footer>"
    )

    with pytest.raises(ArticleExtractionError, match="body paragraphs"):
        parse_beograd_article(html, load_source_file(SOURCE_PATH), ARTICLE_URL)


@pytest.mark.asyncio
async def test_belgrade_article_collector_fetches_and_extracts() -> None:
    html = (FIXTURES / "beograd_article.html").read_text(encoding="utf-8")
    source = load_source_file(SOURCE_PATH)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == ARTICLE_URL
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        item = await BelgradeArticleCollector(source, client).collect_url(ARTICLE_URL)

    assert item.external_id == "116232"
    assert item.url == ARTICLE_URL
