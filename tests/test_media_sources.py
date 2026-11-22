from pathlib import Path

import pytest

from relocant_media.collectors.venue_feed import parse_venue_feed
from relocant_media.domain.originality import copies_source_text
from relocant_media.domain.policy import primary_evidence_kind
from relocant_media.ops.server_cycle import discovery_venue_sources, media_sources, venue_sources
from relocant_media.sources.registry import load_source_registry

SOURCES = Path("cities/belgrade/sources")
MEDIA = {
    "media_rts": "media_rts_feed.xml",
    "media_n1_srbija": "media_n1_feed.xml",
    "media_balkan_insight": "media_balkan_insight_feed.xml",
    "media_nova": "media_nova_feed.xml",
    "media_danas": "media_danas_feed.xml",
}


def _registry():
    return {source.id: source for source in load_source_registry(SOURCES)}


def test_verified_media_follow_the_normal_news_path_and_are_never_high_risk() -> None:
    registry = _registry()
    assert set(media_sources()) == {*MEDIA, "media_aero_telegraf"}
    aviation = registry["media_aero_telegraf"]
    assert aviation.ownership == "media" and aviation.categories == ("transport",)
    assert aviation.policy.auto_publish is False
    for source_id in MEDIA:
        source = registry[source_id]
        assert source.ownership == "media" and source.source_type == "news_media"
        assert source.policy.auto_publish is True and source.policy.high_risk is False
        assert source.extraction.strategy == "venue_feed" and source.extraction.status == "active"
        assert 0 < source.trust <= 0.85
        host = str(source.extraction.feed_url).split("/")[2]
        assert host in source.extraction.approved_hosts


def test_media_never_enter_the_official_venue_paths() -> None:
    assert not set(MEDIA) & set(venue_sources())
    assert not set(MEDIA) & set(discovery_venue_sources())


PAGE_TEXT = {"media_nova", "media_danas", "media_n1_srbija"}


@pytest.mark.parametrize(("source_id", "fixture"), MEDIA.items())
def test_real_feed_samples_yield_full_text_items(source_id: str, fixture: str) -> None:
    xml = (Path("tests/fixtures") / fixture).read_text(encoding="utf-8")
    items = parse_venue_feed(xml, _registry()[source_id])
    assert items and all(item.published_at and item.url for item in items)
    if source_id not in PAGE_TEXT:
        assert all(len(item.body) >= 120 for item in items)


def test_media_can_only_support_non_high_risk_topics() -> None:
    assert primary_evidence_kind(["Official", "media"], "law") == "official"
    assert primary_evidence_kind(["media"], "events") == "media"
    assert primary_evidence_kind([" Media "], "transport") == "media"
    assert primary_evidence_kind(["media"], "politics") is None
    assert primary_evidence_kind(["media"], "LAW") is None
    assert primary_evidence_kind(["unknown"], "events") is None


def test_verbatim_copy_is_detected_but_translation_and_retelling_pass() -> None:
    source = ("Gradska vlada je saopštila da će od ponedeljka biti obustavljen saobraćaj "
              "u ulici Kralja Milana zbog radova na vodovodnoj mreži u trajanju od tri dana")
    copied = "Важно: " + source + " — сообщили власти."
    assert copies_source_text(copied, [source]) is True
    retold = ("С понедельника на три дня перекроют улицу Кралja Милана: идёт ремонт водопровода, "
              "власти советуют ехать в объезд.")
    assert copies_source_text(retold, [source]) is False
    assert copies_source_text("", [source]) is False
    assert copies_source_text(copied, []) is False


def test_teaser_only_feeds_fetch_the_article_page_for_evidence() -> None:
    import asyncio
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from relocant_media.collectors.venue_feed import VenueFeedCollector

    registry = _registry()
    assert registry["media_nova"].extraction.page_text
    assert not registry["media_rts"].extraction.page_text

    teaser = "Kratak anons vesti koji je dovoljno dug da pre izmene ostane jedini dokaz. " * 3
    today = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    feed = (f"<rss><channel><item><title>Vest</title><link>https://nova.rs/vesti/a</link>"
            f"<description>{teaser}</description><pubDate>{today}</pubDate></item></channel></rss>")
    page = ("<html><head><title>Vest</title></head><body><h1>Vest</h1><article>"
            + "Pun tekst clanka sa svim detaljima. " * 20 + "</article></body></html>")
    collector = VenueFeedCollector(registry["media_nova"], AsyncMock())
    collector._fetch = AsyncMock(side_effect=[feed, page])  # type: ignore[method-assign]

    items = asyncio.run(collector.collect())

    assert len(items) == 1 and "Pun tekst clanka" in items[0].body
    assert teaser.strip() not in items[0].body


def test_danas_publication_header_does_not_expire_upcoming_event() -> None:
    from datetime import UTC, datetime

    from relocant_media.collectors.venue_feed import _article
    from relocant_media.domain.freshness import freshness_decision

    source = _registry()["media_danas"]
    url = "https://www.danas.rs/sport/a/"
    page = (
        '<meta property="article:published_time" content="2026-11-18T16:27:01Z">'
        '<article><h1>Beogradski polumaraton u nedelju</h1>'
        '<div class="post-meta">Beta 18.11.2026. 17:27</div>'
        '<div class="post-content content"><p>Sedmi Beogradski polumaraton, koji će se '
        'održati 22. novembra, imaće više od 4.000 učesnika. Start je na Novom Beogradu.'
        '</p></div></article>'
    )
    item = _article(page, url, source, fallback=None)
    assert "18.11.2026." not in item.body
    assert item.published_at == datetime(2026, 11, 18, 16, 27, 1, tzinfo=UTC)
    assert freshness_decision(
        item.title, item.body, item.published_at, datetime(2026, 11, 19, 9, tzinfo=UTC),
    ).action == "keep"


@pytest.mark.asyncio
async def test_n1_uses_full_main_article_even_when_feed_and_jsonld_have_only_teasers():
    import json
    from datetime import UTC, datetime

    import httpx

    from relocant_media.collectors.venue_feed import VenueFeedCollector

    source = _registry()['media_n1_srbija']
    assert source.extraction.page_text
    url = 'https://n1info.rs/vesti/flight-delays/'
    date = datetime.now(UTC).replace(microsecond=0).isoformat()
    teaser = 'Teaser without the operational details. ' * 5
    feed = (f'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>'
            f'<title>Flight delays</title><link>{url}</link><pubDate>{date}</pubDate>'
            f'<content:encoded><![CDATA[<p>{teaser}</p>]]></content:encoded>'
            '</item></channel></rss>')
    ld = json.dumps({'@type': 'NewsArticle', 'headline': 'Flight delays',
                     'datePublished': date, 'articleBody': teaser})
    full = 'Full operational details and verified contact information. ' * 5
    page = (f'<script type="application/ld+json">{ld}</script>'
            '<article class="article-wrapper"><p>Air Serbia statement.</p>'
            f'<div class="article-content-wrapper"><div class="rich-text-block"><p>{full}</p>'
            '</div></div><div class="article-content-wrapper"><article>'
            '<p>Unrelated recommended story.</p></article></div></article>')
    requested = []

    def handle(request):
        requested.append(str(request.url))
        return httpx.Response(200, text=feed if request.url.path == '/feed/' else page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        item = await VenueFeedCollector(source, client).collect_url(url)
    assert requested == [source.extraction.feed_url, url]
    assert full.strip() in item.body and 'Air Serbia statement.' in item.body
    assert teaser.strip() not in item.body and 'Unrelated recommended' not in item.body


def test_n1_video_programmes_are_not_treated_as_text_articles():
    from datetime import UTC, datetime

    from relocant_media.collectors.venue_feed import _allowed

    source = _registry()['media_n1_srbija']
    date = datetime.now(UTC).isoformat()
    base = 'https://n1info.rs/'
    xml = '<rss><channel>' + ''.join(
        f'<item><title>Test</title><link>{base}{path}</link><pubDate>{date}</pubDate></item>'
        for path in ('video/dnevnik/example/', 'vesti/flight-delays/')
    ) + '</channel></rss>'
    assert [item.url for item in parse_venue_feed(xml, source)] == [base + 'vesti/flight-delays/']
    with pytest.raises(ValueError, match='content path'):
        _allowed(base + 'video/dnevnik/example/', source)


@pytest.mark.parametrize(
    ('source_id', 'base', 'excluded', 'kept'),
    [
        ('media_n1_srbija', 'https://n1info.rs/',
         ('najave-emisija/gledajte-uzivo-dnevnik-u-19/', 'svet/izbori-u-izraelu/',
          'region/bih-vlada/', 'english/news/rem-licence/'),
         ('vesti/beograd-voda/', 'biznis/cene-goriva/', 'sport/polumaraton/')),
        ('media_danas', 'https://www.danas.rs/',
         ('svet/misija-evropskog-parlamenta/', 'vesti/politika/skupstina-sednica/'),
         ('vesti/drustvo/gimnazija/', 'sport/beogradski-polumaraton/', 'kultura/izlozba/')),
        ('media_nova', 'https://nova.rs/',
         ('vesti/svet/netanjahu/', 'vesti/politika/izbori/'),
         ('vesti/drustvo/gimnazija/', 'kultura/program/', 'sport/fudbal/derbi/')),
        ('media_rts', 'https://www.rts.rs/',
         ('vesti/svet/6070001', 'lat/vesti/svet/6070002', 'vesti/politika/6070684',
          'lat/vesti/politika/6070685'),
         ('vesti/hronika/6070572', 'lat/vesti/beograd/6070003', 'lat/vesti/drustvo/6070004')),
    ],
)
def test_national_feeds_skip_world_politics_and_tv_sections_before_any_fetch(
    source_id, base, excluded, kept,
):
    # #326/#327: world, regional and political sections of national media never reach
    # the editorial queue; city, society, culture and sport stay (owner, 19.11.2026).
    from datetime import UTC, datetime

    from relocant_media.collectors.venue_feed import _allowed

    source = _registry()[source_id]
    date = datetime.now(UTC).isoformat()
    xml = '<rss><channel>' + ''.join(
        f'<item><title>Test</title><link>{base}{path}</link><pubDate>{date}</pubDate></item>'
        for path in (*excluded, *kept)
    ) + '</channel></rss>'
    assert [item.url for item in parse_venue_feed(xml, source)] == [base + path for path in kept]
    for path in excluded:
        with pytest.raises(ValueError, match='content path'):
            _allowed(base + path, source)
