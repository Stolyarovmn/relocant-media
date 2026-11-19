from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.collectors.beograd_article import parse_beograd_article
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.domain.dedup import EventCandidate
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops import server_cycle
from relocant_media.services.editorial import _event_items, _reviewed_event_item
from relocant_media.services.replacement_identity import matches_replacement_event
from relocant_media.services.source_replacement import SourceUnavailableError
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source


def test_same_named_opening_requires_identical_date_and_clock():
    original = EventCandidate(
        1, "media", "https://media.example/signal", "Otvara se novi prostor",
        'Otvaranje izložbe "Svetlosti modernog gradskog slikarstva" 20. novembra u 19 sati.',
        datetime(2026, 11, 17, 15, tzinfo=UTC),
    )
    official = EventCandidate(
        2, "official", "https://official.example/announce", "Najava novog programa",
        'Izložbe „Svetlosti modernog gradskog slikarstva”, u petak, 20. novembra, u 19 sati. '
        'Prijem je u sali 4.',
        datetime(2026, 11, 19, 8, tzinfo=UTC),
    )
    assert matches_replacement_event(original, official)
    assert not matches_replacement_event(original, replace(
        official, body=official.body.replace("19 sati", "20 sati"),
    ))
    assert not matches_replacement_event(original, replace(
        official, body=official.body.replace("20. novembra", "21. novembra"),
    ))
    assert not matches_replacement_event(original, replace(
        official, body=f"Otkazano. {official.body}",
    ))


def test_city_arts_parser_preserves_publish_time_and_excludes_calendar_widget():
    config = next(source for source in server_cycle.load_source_registry(server_cycle.SOURCES)
                  if source.id == "city_beograd_news")
    url = ("https://www.beograd.rs/lat/zivot-u-beogradu/umetnicki-program/"
           "a116694/galerija.html")
    article = parse_beograd_article(
        "<article><h1>Otvaranje izložbe</h1><span class='story__time'>19.11.2026.</span>"
        "<p>Otvaranje će biti 20. novembra u 19 sati.</p>"
        "<div class='bg-life-info-box'><p>21. februar</p></div></article>", config, url,
    )
    assert article.external_id == "116694"
    assert article.published_at is not None and article.published_at.day == 19
    assert article.published_at_precision == "day"
    assert article.body == "Otvaranje će biti 20. novembra u 19 sati."


@pytest.mark.asyncio
async def test_exact_page_fetch_can_read_live_article_while_rss_is_unavailable():
    config = next(source for source in server_cycle.load_source_registry(server_cycle.SOURCES)
                  if source.id == "media_nova")
    direct = "https://nova.rs/kultura/stara-galerija/"
    calls = []

    def response(request):
        calls.append(str(request.url))
        if str(request.url) != direct:
            return httpx.Response(503)
        return httpx.Response(200, text=(
            '<meta property="article:published_time" content="2026-11-17T13:55:14Z">'
            '<h1>Gradska galerija otvara novu salu</h1><article><p>'
            + "Nova sala prikazuje dela iz gradske umetničke zbirke. " * 4
            + "</p></article>"
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        item = await VenueFeedCollector(config, client).collect_url(direct, require_page=True)
    assert calls == [config.extraction.feed_url, direct]
    assert item.url == direct and item.published_at is not None


@pytest.mark.parametrize("failure", [SourceUnavailableError("media_nova"), ValueError("parser")])
def test_gemini_retries_only_confirmed_unavailability_on_official_raw(
    tmp_path, monkeypatch, failure,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'local.sqlite'}")
    Base.metadata.create_all(engine)
    configs = {source.id: source
               for source in server_cycle.load_source_registry(server_cycle.SOURCES)}
    now = datetime.now(UTC) - timedelta(minutes=5)
    original = CollectedItem(
        "media_nova", "original", "https://nova.rs/kultura/program/", "Otvara se novi prostor",
        "Gradska galerija otvara novi prostor na Kneza Mihaila 36.", now,
    )
    alternative = replace(
        original, source_id="city_beograd_news", external_id="116694",
        url=("https://www.beograd.rs/lat/zivot-u-beogradu/umetnicki-program/"
             "a116694/galerija.html"),
    )
    with Session(engine) as session:
        for item in (original, alternative):
            sync_source(session, "belgrade", configs[item.source_id])
        assert capture_collected_batch(session, [original, alternative]) == (2, 0)
        mapping = dict(session.execute(select(RawItem.source_id, RawItem.id)).all())
        session.commit()
    original_id = mapping[original.source_id]
    replacement_id = mapping[alternative.source_id]
    calls = []

    def prepare(db, raw_id, provider, output):
        calls.append(raw_id)
        if raw_id == original_id:
            raise failure
        with Session(db) as session:
            anchor = session.get(RawItem, raw_id)
            source = session.get(Source, anchor.source_id)
            assert _reviewed_event_item(session, anchor=anchor, anchor_source=source) is None
            members = _event_items(session, anchor=anchor, anchor_source=source)
            assert [row.url for row, _ in members] == [alternative.url]
        return {"raw_item_id": raw_id, "status": "ready"}

    async def fetch(config, url, client):
        assert config.id == alternative.source_id and url == alternative.url
        return alternative

    monkeypatch.setattr(server_cycle, "_prepare_reviewed", prepare)
    monkeypatch.setattr(server_cycle, "fetch_public_article", fetch)
    try:
        if isinstance(failure, SourceUnavailableError):
            result = server_cycle.prepare_reviewed(engine, original_id, None, tmp_path)
            assert result["raw_item_id"] == replacement_id
            assert calls == [original_id, replacement_id]
            with Session(engine) as session:
                marker = session.get(EditorialRun, original_id)
                assert marker.post_id is None
                assert marker.outcome["replacement_raw_item_id"] == replacement_id
                assert session.get(RawItem, original_id).url == original.url
        else:
            with pytest.raises(ValueError, match="parser"):
                server_cycle.prepare_reviewed(engine, original_id, None, tmp_path)
            assert calls == [original_id]
    finally:
        engine.dispose()
