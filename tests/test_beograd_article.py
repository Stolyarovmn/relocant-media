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
TRANSPORT_URL = (
    "https://www.beograd.rs/lat/servisne-informacije/saobracaj-i-prevoz/"
    "a116238/Informacija-o-radovima-u-ulici-Slanacki-put.html"
)
WATER_URL = (
    "https://www.beograd.rs/lat/servisne-informacije/vodovod-i-kanalizacija/"
    "a116702/test.html"
)


@pytest.mark.parametrize("tag", ["p", "h3"])
def test_city_water_bulletin_preserves_outage_windows_and_omits_shared_tankers(tag: str) -> None:
    html = f"""<article>
    <h1>Kvarovi na vodovodnoj mreži biće otklonjeni tokom dana</h1>
    <time class="story__time">18.11.2026.</time>
    <p>Do 15.00</p><ul><li>Zemun: Probna 4</li></ul>
    <p>Do 22.00</p><ul><li>Zvezdara: Primerna 12</li></ul>
    <{tag}>Raspored auto cisterni:</{tag}><ul><li>Voždovac: Treća 6</li></ul>
    </article>"""
    item = parse_beograd_article(html, load_source_file(SOURCE_PATH), WATER_URL)

    assert item.body == (
        "18.11.2026.\n\nDo 15.00\n\nZemun: Probna 4\n\n"
        "Do 22.00\n\nZvezdara: Primerna 12"
    )
    assert item.external_id == "116702"
    assert item.url == WATER_URL
    assert item.published_at == datetime(2026, 11, 18, tzinfo=ZoneInfo("Europe/Belgrade"))


def test_city_dedicated_water_notice_keeps_its_tanker_details() -> None:
    html = """<article><h1>Bez vode u Zemunu zbog popravke</h1>
    <p>Planirana popravka u Probnoj ulici.</p>
    <p>Raspored auto cisterni:</p><ul><li>Zemun: Probna 4</li></ul></article>"""
    item = parse_beograd_article(html, load_source_file(SOURCE_PATH), WATER_URL)

    assert "Raspored auto cisterni:\n\nZemun: Probna 4" in item.body


def test_parse_transport_service_article_preserves_url_and_id() -> None:
    html = (FIXTURES / "beograd_article_nuxt.html").read_text(encoding="utf-8")
    item = parse_beograd_article(html, load_source_file(SOURCE_PATH), TRANSPORT_URL)

    assert item.external_id == "116238"
    assert item.url == TRANSPORT_URL
    assert item.body.startswith("Najavljeni su novi projekti.")


@pytest.mark.parametrize(
    "url",
    [
        TRANSPORT_URL.replace("www.beograd.rs", "foreign.example"),
        TRANSPORT_URL.replace("www.beograd.rs", "www.beograd.rs.foreign.example"),
        TRANSPORT_URL.replace("saobracaj-i-prevoz", "unknown-section"),
        TRANSPORT_URL.replace("/lat/", "/prefix/lat/"),
        TRANSPORT_URL.replace("https://", "http://"),
        TRANSPORT_URL.replace("www.beograd.rs", "user@www.beograd.rs"),
    ],
)
def test_article_rejects_foreign_hosts_and_unsupported_paths(url: str) -> None:
    with pytest.raises(ArticleExtractionError, match="unsupported article url"):
        parse_beograd_article("<main><h1>Title</h1><p>Body</p></main>",
                             load_source_file(SOURCE_PATH), url)


@pytest.mark.asyncio
async def test_article_collector_rejects_foreign_host_before_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("unsupported URL must not be requested")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArticleExtractionError, match="unsupported article url"):
            await BelgradeArticleCollector(load_source_file(SOURCE_PATH), client).collect_url(
                TRANSPORT_URL.replace("www.beograd.rs", "foreign.example")
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
    assert item.published_at_precision == "day"
    assert item.source_html is None


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
    assert item.source_html == html
    assert item.approved_image_hosts == ("static.beograd.rs",)


@pytest.mark.parametrize("visible_date", ["23.11.2026.", "Дата неизвестна"])
def test_water_bulletin_quotes_only_a_parseable_visible_date(visible_date: str) -> None:
    html = f"""<article><h1>Kvarovi na vodovodnoj mreži biće otklonjeni tokom dana</h1>
    <time class="story__time">{visible_date}</time><p>Do 15.00</p>
    <p>Stari grad: Venizelosova 15</p></article>"""
    item = parse_beograd_article(html, load_source_file(SOURCE_PATH), WATER_URL)
    if visible_date == "23.11.2026.":
        assert item.body.startswith("23.11.2026.\n\nDo 15.00")
        assert item.published_at == datetime(2026, 11, 23, tzinfo=ZoneInfo("Europe/Belgrade"))
    else:
        assert item.body == "Do 15.00\n\nStari grad: Venizelosova 15"
        assert item.published_at is None
