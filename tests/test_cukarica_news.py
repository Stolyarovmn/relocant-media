from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors.cukarica_news import (
    CukaricaExtractionError,
    CukaricaNewsCollector,
    parse_cukarica_article,
    parse_cukarica_news_list,
)
from relocant_media.sources.registry import load_source_file

FIXTURES = Path(__file__).parent / "fixtures"
ARTICLE_URL = "https://www.cukarica.rs/vest/655-poziv-za-izlagace-na-cukarickom-festivalu-zimnice/"


def source():
    original = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    return original.model_copy(update={
        "id": "cukarica_official",
        "url": "https://www.cukarica.rs/",
        "extraction": original.extraction.model_copy(update={"strategy": "cukarica_news"}),
    })


def article_html():
    return (FIXTURES / "cukarica_article.html").read_text(encoding="utf-8")


def list_html():
    return (FIXTURES / "cukarica_news.html").read_text(encoding="utf-8")


def test_article_keeps_lead_full_body_and_local_publication_time():
    item = parse_cukarica_article(article_html(), source(), ARTICLE_URL)
    assert item.external_id == "655"
    assert item.url == ARTICLE_URL
    assert item.source_id == "cukarica_official"
    assert "19. до 23. октобра" in item.body
    assert "064/8718-468" in item.body
    assert "Подели вест" not in item.body
    assert "Сличне вести" not in item.body
    assert item.published_at == datetime(2026, 10, 9, 14, 21, tzinfo=ZoneInfo("Europe/Belgrade"))


def test_list_extracts_ids_titles_dates_and_not_truncated_evidence():
    items = parse_cukarica_news_list(list_html(), source())
    assert [item.external_id for item in items] == ["668", "669", "667"]
    assert all(item.body == "" for item in items)
    assert items[0].published_at == datetime(2026, 10, 17, tzinfo=ZoneInfo("Europe/Belgrade"))


@pytest.mark.parametrize("old,new", [
    ('class="page-content"', 'class="unknown"'),
    ('<!-- Excerpt -->', '<!-- changed -->'),
    ('09.10.2026 14:21', 'yesterday'),
    ('<h1 ', '<h2 '),
])
def test_article_template_changes_fail_closed(old, new):
    with pytest.raises(CukaricaExtractionError):
        parse_cukarica_article(article_html().replace(old, new), source(), ARTICLE_URL)


@pytest.mark.parametrize("old,new", [
    ('class="news-grid"', 'class="unknown"'),
    ('class="news-grid-title"', 'class="unknown"'),
    ('17.10.2026', '32.10.2026'),
    ('/vest/668-', 'https://foreign.example/vest/668-'),
])
def test_list_template_changes_and_external_links_fail_closed(old, new):
    with pytest.raises(CukaricaExtractionError):
        parse_cukarica_news_list(list_html().replace(old, new), source())


@pytest.mark.parametrize("url", [
    ARTICLE_URL.replace("https://", "http://"),
    ARTICLE_URL.replace("www.cukarica.rs", "www.cukarica.rs.evil.example"),
    ARTICLE_URL.replace("www.cukarica.rs", "user@www.cukarica.rs"),
    ARTICLE_URL.replace("www.cukarica.rs", "www.cukarica.rs:444"),
    ARTICLE_URL.replace("/vest/", "/admin/"),
    ARTICLE_URL.replace("655-", "../655-"),
])
@pytest.mark.asyncio
async def test_invalid_urls_never_issue_network_requests(url):
    def handler(request):
        pytest.fail("unexpected request")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CukaricaExtractionError):
            await CukaricaNewsCollector(source(), client).collect_url(url)


@pytest.mark.asyncio
async def test_collect_fetches_all_full_articles():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, text=list_html() if request.url.path == "/" else article_html())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await CukaricaNewsCollector(source(), client).collect()
    assert len(requests) == 4
    assert len(items) == 3
    assert all("19. до 23. октобра" in item.body for item in items)
    assert [item.external_id for item in items] == ["668", "669", "667"]


@pytest.mark.asyncio
async def test_external_redirect_is_rejected_before_following():
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.example/stolen"})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(CukaricaExtractionError, match="unsupported Cukarica URL"):
            await CukaricaNewsCollector(source(), client).collect_url(ARTICLE_URL)
    assert requests == [ARTICLE_URL]


@pytest.mark.asyncio
async def test_redirect_to_different_article_is_rejected():
    def handler(request):
        if request.url.path == "/vest/655-poziv-za-izlagace-na-cukarickom-festivalu-zimnice/":
            return httpx.Response(302, headers={"Location": "/vest/999-another/"})
        return httpx.Response(200, text=article_html())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CukaricaExtractionError, match="changed article ID"):
            await CukaricaNewsCollector(source(), client).collect_url(ARTICLE_URL)
