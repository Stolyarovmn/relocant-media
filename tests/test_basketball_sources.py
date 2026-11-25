from datetime import UTC, datetime, time, timedelta
from email.utils import format_datetime

import httpx
import pytest

from relocant_media.collectors.venue_feed import (
    OutsideArticleScope,
    VenueFeedCollector,
    parse_article_index,
)
from relocant_media.domain.publication_time import BELGRADE
from relocant_media.ops.gpt_export_receipt import PUBLIC_SOURCE_TYPES
from relocant_media.ops.server_cycle import fetch_public_article, sports_sources
from relocant_media.services.gpt_refresh import _registered_alternative
from relocant_media.services.primary_discovery import source_for_url

ZVEZDA = "https://kkcrvenazvezda.rs/news/raspored-utakmica"
PARTIZAN = "https://www.partizan.basketball/vesti/aktuelno/4296"
VIRTUS = "https://www.virtus.it/news/euroleague-round-14-crvena-zvezda-vs-virtus-bologna-84-108/"
SPORT_TEXT = (
    "Košarkaši su odigrali utakmicu u Beogradskoj areni. Organizator je objavio termin "
    "otvaranja kapija, raspored prilaza dvorani i korisna uputstva za posetioce utakmice."
)


def test_sports_registry_supports_exact_news_and_excludes_index_pages():
    sources = sports_sources()
    assert set(sources) == {
        "basketball_crvena_zvezda", "basketball_partizan", "basketball_virtus_belgrade",
    }
    for url, source_id in [
        (ZVEZDA, "basketball_crvena_zvezda"),
        (PARTIZAN, "basketball_partizan"),
        (VIRTUS, "basketball_virtus_belgrade"),
    ]:
        assert source_for_url(url, sources) == source_id
        assert _registered_alternative(url, sources).id == source_id
        assert sources[source_id].source_type in PUBLIC_SOURCE_TYPES
    assert source_for_url("https://www.partizan.basketball/vesti/aktuelno", sources) is None
    assert source_for_url("https://kkcrvenazvezda.rs/calendar", sources) is None
    assert source_for_url("https://www.virtus.it/feed/", sources) is None


def zvezda_index(rows: str) -> str:
    return f'<section class="Active">{rows}</section>'


def zvezda_row(url: str, day: str) -> str:
    return f'<div class="Column_column__demo"><span>{day}</span><a href="{url}">Вест</a></div>'


def test_article_index_uses_one_publication_day_and_one_canonical_link():
    source = sports_sources()["basketball_crvena_zvezda"]
    article_path = "/news/raspored-utakmica"
    html = zvezda_index(
        zvezda_row(article_path, "23.11.2026.")
        + zvezda_row(article_path + "?lang=sr", "23.11.2026.")
        + zvezda_row("https://example.net/news/fake", "23.11.2026.")
        + zvezda_row("/calendar", "23.11.2026.")
        + zvezda_row("/news/invalid-date", "31.11.2026.")
    )
    item, = parse_article_index(html, source)
    assert item.url == item.external_id == ZVEZDA
    assert item.published_at == datetime(2026, 11, 22, 23, tzinfo=UTC)
    assert item.published_at_precision == "day"
    with pytest.raises(ValueError, match="no dated article links"):
        parse_article_index(zvezda_index(zvezda_row(article_path, "Sutra u 19:00")), source)


@pytest.mark.asyncio
async def test_partizan_exact_lookup_uses_index_day_and_full_article():
    source = sports_sources()["basketball_partizan"]
    day = datetime.now(BELGRADE).date() - timedelta(days=1)
    index = f"""
      <div class="news-container"><div class="nc-col item">
        <div class="content"><h5 class="date">{day:%d.%m.%Y}.</h5></div>
        <a class="more" href="{PARTIZAN}">Pročitaj više</a>
      </div></div>
    """
    article = f"""
      <div class="single-news-wrapper"><h1>Najava utakmice</h1>
        <div class="page-content"><p>{SPORT_TEXT}</p></div>
      </div>
    """
    paths = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.url.host == "www.partizan.basketball"
        return httpx.Response(200, text=article if request.url.path.endswith("/4296") else index)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        item = await fetch_public_article(
            source, "https://partizan.basketball/vesti/aktuelno/4296?lang=sr", client,
        )
    assert paths == ["/vesti/aktuelno", "/vesti/aktuelno/4296"]
    assert item.url == item.external_id == PARTIZAN
    assert item.title == "Najava utakmice" and item.body == SPORT_TEXT
    assert item.published_at == datetime.combine(day, time.min, BELGRADE).astimezone(UTC)
    assert item.published_at_precision == "day"


@pytest.mark.asyncio
async def test_virtus_result_is_exact_and_requires_a_belgrade_club():
    source = sports_sources()["basketball_virtus_belgrade"]
    published = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    title = "Euroleague | Crvena Zvezda vs Virtus Bologna | 84-108"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/feed/":
            return httpx.Response(503)
        return httpx.Response(200, text=f"""
          <meta property="article:published_time" content="{published}">
          <h1>{title}</h1>
          <div class="bigslam-single-article-content">
            <p>Crvena Zvezda vs Virtus Bologna: 84-108</p><p>{SPORT_TEXT}</p>
          </div>
        """)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await fetch_public_article(source, VIRTUS, client)
        assert "84-108" in result.body and result.published_at_precision == "instant"
        title = "Virtus Bologna vs Milano"
        with pytest.raises(OutsideArticleScope):
            await fetch_public_article(source, VIRTUS, client)


@pytest.mark.asyncio
async def test_virtus_only_fetches_belgrade_games_from_the_rss():
    source = sports_sources()["basketball_virtus_belgrade"]
    now = datetime.now(UTC) - timedelta(hours=1)
    title = "Euroleague | Crvena Zvezda vs Virtus Bologna | 84-108"
    feed = f"""
      <rss version="2.0"><channel><title>Virtus</title>
        <item><title>Virtus Bologna vs Milano</title>
          <link>https://www.virtus.it/news/milano/</link>
          <pubDate>{format_datetime(now)}</pubDate></item>
        <item><title>{title}</title><link>{VIRTUS}</link>
          <pubDate>{format_datetime(now)}</pubDate></item>
      </channel></rss>
    """
    accessed = []

    def respond(request: httpx.Request) -> httpx.Response:
        accessed.append(str(request.url))
        if request.url.path == "/feed/":
            return httpx.Response(200, text=feed)
        assert str(request.url) == VIRTUS
        return httpx.Response(200, text=f"""
          <h1>{title}</h1>
          <div class="bigslam-single-article-content">
            <p>Crvena Zvezda vs Virtus Bologna: 84-108</p><p>{SPORT_TEXT}</p>
          </div>
        """)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        item, = await VenueFeedCollector(source, client).collect()
    assert item.url == VIRTUS and "84-108" in item.body
    assert accessed == ["https://www.virtus.it/feed/", VIRTUS]
