from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ingestion.persistence import sync_source
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.editorial import _auto_publish_policy_reason, _event_items
from relocant_media.services.gpt_fallback import build_request
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base, RawItem, Source

NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)
SOURCES = Path("cities/belgrade/sources")
VENUE_URL = "https://bitefartcafe.rs/dogadjaji/severni-jazz/"
ANNOUNCEMENT = (
    "Severni Jazz Orkestar stiže u BitefArtCafe 27. novembra. "
    "Pet muzičara iz tri zemlje predstaviće zajednički program sa novim aranžmanima "
    "nastalim tokom dvogodišnje saradnje i putovanja kroz gradove regiona."
)
DETAILS = "Vrata otvaramo u 20:30. Lokacija: Mitropolita Petra 8. Cena Ulaznice: od 3.000rsd."


@pytest.fixture
def venue_bundle():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for key in ("media_nova", "venue_bitefartcafe"):
            sync_source(session, "belgrade", load_source_file(SOURCES / f"{key}.yaml"))
        article = RawItem(
            source_id="media_nova", external_id="nova-concert",
            url="https://nova.rs/kultura/severni-jazz-orkestar/",
            title="Severni Jazz Orkestar stiže u Beograd",
            body=f"23. novembra 2026.\n{ANNOUNCEMENT}",
            published_at=NOW - timedelta(minutes=20), published_at_precision="instant",
            ingested_at=NOW,
        )
        venue = RawItem(
            source_id="venue_bitefartcafe", external_id=VENUE_URL, url=VENUE_URL,
            title="SEVERNI JAZZ ORKESTAR feat. Session Quartet",
            body=f"Datum događaja: 27.11.2026.\n{ANNOUNCEMENT}\n{DETAILS}",
            published_at=None, ingested_at=NOW,
        )
        session.add_all((article, venue))
        session.flush()
        yield session, article, venue
    engine.dispose()


def _members(session, article):
    source = session.get(Source, article.source_id)
    return _event_items(session, anchor=article, anchor_source=source)


def test_official_card_adds_support_without_becoming_a_publishable_anchor(venue_bundle):
    session, article, venue = venue_bundle
    members = _members(session, article)
    assert [item.id for item, _ in members] == [article.id, venue.id]
    assert members[1][0].published_at is None
    assert DETAILS in members[1][0].body
    venue_members = _members(session, venue)
    assert [item.id for item, _ in venue_members] == [venue.id]
    assert _auto_publish_policy_reason(
        venue_members, anchor_source=venue_members[0][1], category="events",
    ) == "source_auto_publish_disabled"


@pytest.mark.parametrize(
    "change", ["date", "venue", "title", "changes", "ambiguous", "host", "oversized"],
)
def test_mismatched_or_ambiguous_official_card_stays_out_of_the_package(venue_bundle, change):
    session, article, venue = venue_bundle
    if change == "date":
        article.body = article.body.replace("27. novembra", "28. novembra")
    if change == "venue":
        article.body = article.body.replace("BitefArtCafe", "Zappa Baza")
    if change == "title":
        venue.title = "Južni Rock Bend"
    if change == "changes":
        article.title += ": koncert je odložen"
    if change == "host":
        venue.url = "https://example.test/dogadjaji/severni-jazz/"
    if change == "oversized":
        venue.body += " " * 20_001
    if change == "ambiguous":
        session.add(RawItem(
            source_id=venue.source_id, external_id="second-session",
            url="https://bitefartcafe.rs/dogadjaji/severni-jazz-2/",
            title=venue.title, body=venue.body, ingested_at=venue.ingested_at,
        ))
    session.flush()
    assert [item.id for item, _ in _members(session, article)] == [article.id]


@pytest.mark.parametrize("event_date_in_announcement", [False, True])
def test_publication_date_alone_cannot_match_a_same_day_event(
    venue_bundle, event_date_in_announcement,
):
    session, article, venue = venue_bundle
    announcement = ANNOUNCEMENT.replace("27. novembra", "23. novembra")
    venue.body = f"Datum događaja: 23.11.2026.\n{announcement}\n{DETAILS}"
    if not event_date_in_announcement:
        announcement = announcement.replace(" 23. novembra", "")
    article.body = f"23. novembra 2026.\n{announcement}"
    session.flush()
    expected = [article.id, venue.id] if event_date_in_announcement else [article.id]
    assert [item.id for item, _ in _members(session, article)] == expected


@pytest.mark.parametrize("rescheduled", [False, True])
def test_gpt_exports_the_same_exact_documents_for_the_same_announcement(venue_bundle, rescheduled):
    session, article, venue = venue_bundle
    if rescheduled:
        venue.body += (
            "\nKoncert, planiran za 26. novembar, iz organizacionih razloga, "
            "pomera na 27. novembar."
        )
    session.add(SchedulerRun(
        task_key="editorial-primary:belgrade", task_type="editorial_primary",
        city="belgrade", status="partial", scheduled_for=NOW - timedelta(minutes=10),
        started_at=NOW - timedelta(minutes=10), finished_at=NOW - timedelta(minutes=5),
        details={"processing_errors": [
            {"raw_item_id": article.id, "error_type": "GeminiWorkerError"},
        ]},
    ))
    session.flush()
    request = build_request(session, now=NOW)
    assert request is not None
    documents = request["candidates"][0]["documents"]
    assert [doc["raw_item_id"] for doc in documents] == [article.id, venue.id]
    assert documents[1]["published_at"] is None
    assert documents[1]["auto_publish"] is False
    assert DETAILS in documents[1]["body"]


@pytest.mark.parametrize("new_day,extra", [("28", ""), ("27", " Koncert je otkazan.")])
def test_ambiguous_reschedule_does_not_pair(venue_bundle, new_day, extra):
    session, article, venue = venue_bundle
    venue.body += f"\nKoncert, planiran za 26. novembar, pomera na {new_day}. novembar.{extra}"
    session.flush()
    assert [item.id for item, _ in _members(session, article)] == [article.id]
