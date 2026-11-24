import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.package import PackageEventDocument
from relocant_media.collectors.base import RawItem as Collected
from relocant_media.collectors.linked_tour_map import (
    SOURCE_ID,
    TourMapUnavailable,
    parse_tour_place,
    tour_links,
)
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops import server_cycle
from relocant_media.services.editorial import _event_items, _snapshot_member
from relocant_media.services.editorial_products import build_presentation
from relocant_media.services.primary_discovery import source_for_url
from relocant_media.sources.registry import load_source_registry
from relocant_media.storage.models import Base, RawItem, Source

CITY = Path("cities/belgrade")
LINK, = tour_links(CITY)
HTML = Path("tests/fixtures/platinumlist_tour_map.html").read_text(encoding="utf-8")
ORGANIZER = f"""
<link rel="canonical" href="{LINK.organizer}">
<title>Kreator with Special Guests Overkill</title>
<div class="t-rec">
  <a href="{LINK.map_ticket}">Buy ticket</a> Nov 25 Belgrade, Serbia Hangar Luka Beograd 2026
</div>
"""
NOW = datetime(2026, 11, 24, 11, tzinfo=UTC)
ARTICLE = Collected(
    "media_danas", "tour1", "https://www.danas.rs/kultura/kreator-overkill-koncert-sve-spremno/",
    "Kreator i Overkill u Beogradu",
    "Koncert će se održati 25. novembra 2026. godine u Hangaru Luke Beograd. "
    f"Ulaznice: {LINK.article_ticket} a satnica je sledeća.",
    NOW - timedelta(hours=2), published_at_precision="instant",
)


@pytest.mark.asyncio
async def test_collects_the_queued_article_and_builds_the_reviewable_mini_app_map():
    registry = {source.id: source for source in load_source_registry(CITY / "sources")}
    media = {ARTICLE.source_id: registry[ARTICLE.source_id]}
    requested = []

    def serve(request):
        url = str(request.url)
        requested.append(url)
        pages = {
            LINK.article_ticket: f"""
              <link rel="canonical" href="{LINK.article_ticket}">
              <h1>KREATOR + OVERKILL</h1>
              <div class="selected-event">
                <p><strong>KREATOR + OVERKILL</strong></p>
                <p><strong>Luka Beograd, Beograd</strong></p>
                <p><strong>25. novembar 2026, sreda 19:00</strong></p>
              </div>""",
            LINK.map_ticket: HTML,
            LINK.organizer: ORGANIZER,
        }
        return httpx.Response(200, text=pages[url])

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for key in (*media, "ticket_vision_events", SOURCE_ID):
            sync_source(session, "belgrade", registry[key])
        capture_collected_batch(session, [ARTICLE])
        queued = server_cycle._queued_tour_articles(session, media, now=NOW)
        assert queued == [ARTICLE]
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            cards = await server_cycle._cited_ticket_evidence(
                queued, media, registry["ticket_vision_events"], client,
                now=NOW, map_config=registry[SOURCE_ID],
            )
        assert set(requested) == {LINK.organizer, LINK.map_ticket, LINK.article_ticket}
        assert {card.source_id for card in cards} == {"ticket_vision_events", SOURCE_ID}
        assert all(card.published_at is None for card in cards)
        capture_collected_batch(session, cards)
        assert server_cycle._queued_tour_articles(session, media, now=NOW) == []

        raw = session.scalar(select(RawItem).where(RawItem.source_id == ARTICLE.source_id))
        source = session.get(Source, ARTICLE.source_id)
        members = _event_items(session, anchor=raw, anchor_source=source)
        assert [item.source_id for item, _ in members] == [ARTICLE.source_id, SOURCE_ID]
        presentation = build_presentation(
            session, raw.id,
            tuple(PackageEventDocument.model_validate_json(json.dumps(_snapshot_member(item, src)))
                  for item, src in members),
            document_ids=[item.id for item, _ in members],
            profile=load_city_profile("belgrade"), rubric_id="weekend",
        )
        place = presentation.event_place
        assert place["starts_at"] == "2026-11-25T19:00:00+01:00"
        assert place["ends_at"] is None
        assert place["venue"] == {
            "name": "Hangar Luka Beograd", "address": "Dunavska 22, Beograd, Serbia",
            "latitude": 44.826035, "longitude": 20.4748069,
        }
        assert place["source_url"] == LINK.map_ticket
        assert presentation.source_map is None


@pytest.mark.parametrize("old,new", [
    ("Nov 25", "Nov 26"),
    ("Belgrade, Serbia", "Novi Sad, Serbia"),
    ("Hangar Luka Beograd", "Sava Center"),
    ("Buy ticket", "Cancelled"),
    ("Kreator with Special Guests Overkill", "Kreator with Special Guests Anthrax"),
])
@pytest.mark.asyncio
async def test_rejects_organizer_changes_and_unregistered_ticket_urls(old, new):
    registry = {source.id: source for source in load_source_registry(CITY / "sources")}
    other_url = LINK.map_ticket.replace("107365", "107366")
    assert source_for_url(LINK.map_ticket, registry) == SOURCE_ID
    assert source_for_url(other_url, registry) is None

    def serve(request):
        html = ORGANIZER.replace(old, new) if str(request.url) == LINK.organizer else HTML
        return httpx.Response(200, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises(TourMapUnavailable):
            await server_cycle.fetch_public_article(registry[SOURCE_ID], LINK.map_ticket, client)
        with pytest.raises(ValueError, match="unapproved tour map address"):
            await server_cycle.fetch_public_article(registry[SOURCE_ID], other_url, client)


@pytest.mark.parametrize("old,new", [
    ("44.8260350,20.4748069", "48.8566000,2.3522000"),
    ("Dunavska 22, Beograd, Serbia", "Dunavska 22, Novi Sad, Serbia"),
    ("Start:", "From:"),
    ("Europe%2FBelgrade", "UTC"),
])
def test_seller_must_provide_exact_location_and_start(old, new):
    with pytest.raises(ValueError):
        parse_tour_place(HTML.replace(old, new), LINK.map_ticket)
