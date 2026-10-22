from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors.srbijavoz_news import (
    SrbijavozExtractionError,
    SrbijavozNewsCollector,
    parse_srbijavoz_article,
    parse_srbijavoz_news_list,
)
from relocant_media.sources.registry import SourceConfig

BELGRADE_TZ = ZoneInfo("Europe/Belgrade")


def _source() -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": "srbijavoz_news",
            "name": "Srbijavoz — Vesti",
            "publisher": "Srbijavoz a.d.",
            "ownership": "official",
            "url": "https://srbijavoz.rs/vesti/",
            "language": "sr-Cyrl",
            "source_type": "official_rail_operator_news",
            "trust": 1.0,
            "check_every": "1h",
            "categories": ["transport", "rail", "city"],
            "extraction": {"strategy": "srbijavoz_news", "status": "active"},
            "policy": {"high_risk": False, "auto_publish": False},
        }
    )


def test_list_extracts_only_linked_article_headings() -> None:
    second_slug = (
        "zatvaranje-stanice-novi-beograd-za-ulazak-i-izlazak-putnika-"
        "od-9-septembra-2026-godine"
    )
    html = f"""
    <main>
      <h1>Вести</h1>
      <div class="card">
        <h3><a href="/srbijavoz-nije-doslo-do-poskupljenja-voznih-karata/">
          Србијавоз: Није дошло до поскупљења возних карата
        </a></h3>
        <a href="/srbijavoz-nije-doslo-do-poskupljenja-voznih-karata/">Прочитај више</a>
      </div>
      <div class="card">
        <a href="/{second_slug}/">
          <h3>Затварање станице Нови Београд за улазак и излазак путника</h3>
        </a>
      </div>
      <h3><a href="https://example.com/not-official/">External</a></h3>
      <h3><a href="/informacije/">Navigation</a></h3>
    </main>
    """

    items = parse_srbijavoz_news_list(html, _source())

    assert [item.external_id for item in items] == [
        "srbijavoz-nije-doslo-do-poskupljenja-voznih-karata",
        second_slug,
    ]
    assert all(item.published_at is None and item.body == "" for item in items)


def test_article_uses_full_body_canonical_and_published_time() -> None:
    url = (
        "https://srbijavoz.rs/zatvaranje-stanice-novi-beograd-za-ulazak-i-"
        "izlazak-putnika-od-9-septembra-2026-godine/"
    )
    html = f"""
    <html><head>
      <link rel="canonical" href="{url}">
      <meta property="article:published_time" content="2026-09-04T08:15:00+02:00">
    </head><body>
      <h1>
        Затварање станице Нови Београд за улазак и излазак путника
        од 9. септембра 2026. године
      </h1>
      <div class="elementor-widget-theme-post-content">
        <p>
          „Србијавоз“ а.д. обавештава кориснике услуге да се станица
          Нови Београд затвара за улазак и излазак путника.
        </p>
        <p>Истовремено се стајалиште Тошин бунар отвара за све категорије возова.</p>
      </div>
    </body></html>
    """

    item = parse_srbijavoz_article(html, _source(), url)

    assert item.external_id.startswith("zatvaranje-stanice-novi-beograd")
    assert item.url == url
    assert item.canonical_url == url
    assert item.published_at == datetime(2026, 9, 4, 8, 15, tzinfo=BELGRADE_TZ)
    assert "Тошин бунар" in item.body
    assert item.source_html == html


def test_article_falls_back_to_visible_serbian_publication_date() -> None:
    url = "https://srbijavoz.rs/srbijavoz-nije-doslo-do-poskupljenja-voznih-karata/"
    html = f"""
    <html><head><link rel="canonical" href="{url}"></head><body>
      <article>
        <h1>Србијавоз: Није дошло до поскупљења возних карата</h1>
        <div>9. октобар 2026. · 1 мин</div>
        <p>„Србијавоз” званично демантује информације о повећању цена возних карата.</p>
        <p>Промене се односе на ред вожње и организацију железничког саобраћаја.</p>
      </article>
    </body></html>
    """

    item = parse_srbijavoz_article(html, _source(), url)

    assert item.published_at == datetime(2026, 10, 9, tzinfo=BELGRADE_TZ)
    assert "организацију" in item.body


def test_article_rejects_missing_publication_date() -> None:
    url = "https://srbijavoz.rs/example-news-item/"
    html = f"""
    <html><head><link rel="canonical" href="{url}"></head><body>
      <article><h1>Example</h1>
      <p>This body is intentionally long enough to be considered article content by parser.</p>
      </article>
    </body></html>
    """

    with pytest.raises(SrbijavozExtractionError, match="publication date"):
        parse_srbijavoz_article(html, _source(), url)


@pytest.mark.asyncio
async def test_collector_validates_redirect_host_before_following() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/article/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = SrbijavozNewsCollector(_source(), client)
        with pytest.raises(SrbijavozExtractionError, match="unsupported Srbijavoz URL"):
            await collector.collect()
