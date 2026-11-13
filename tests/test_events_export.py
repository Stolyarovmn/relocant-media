import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_editorial_package import bind_context, event_payload, parse
from test_event_place import PROFILE, document, html
from test_event_place_adapters import _source

from relocant_media.ai.models import RussianDraft
from relocant_media.ai.package import canonical_json, draft_sha256
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.ops.events_export import export_events, export_events_snapshot, read_events_feed
from relocant_media.ops.map_backfill import plan_event_buttons
from relocant_media.ops.newsroom_publish import EVENTS_PATH, _publish_events
from relocant_media.ops.pages_assets import copy_assets
from relocant_media.services.event_place import resolve_saved_event_place
from relocant_media.services.event_place_adapters import _CUKARICA_POST_2026, _DOB_INTERVIEW_2026
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source, Submission

NOW = datetime(2026, 11, 13, 10, tzinfo=UTC)


def persist_package(run, payload):
    bind_context(payload)
    persisted = parse(payload).model_dump(mode="json")
    run.outcome = {
        "_package": persisted,
        "_package_sha256": hashlib.sha256(canonical_json(persisted).encode()).hexdigest(),
    }


def seed(session, *, doc=None, source_html=None, attested=True):
    doc = doc or document()
    source_html = source_html or html()
    session.add(
        Source(
            id=doc.source_id,
            city="belgrade",
            name="Official test venue",
            url=doc.url,
            ownership="official",
            enabled=True,
            trust=1,
            source_type="official_venue_events",
        )
    )
    session.commit()
    capture_collected_batch(
        session, [CollectedItem(**doc.model_dump(), external_id="concert", source_html=source_html)]
    )
    item = session.scalar(select(RawItem))
    draft = RussianDraft(
        title="Концерт 16 ноября", body="Публичное объявление", claim_ids=("date",)
    )
    payload = event_payload()
    payload.update(
        raw_item_id=item.id,
        source_id=doc.source_id,
        article_id="concert",
        evidence=doc.model_dump(mode="json"),
    )
    payload["event"]["documents"] = [
        {
            **payload["evidence"],
            "raw_item_id": item.id,
            "ownership": "official",
            "high_risk": False,
            "auto_publish": True,
        }
    ]
    payload["outcome"]["ranking"]["category"] = "events"
    payload["outcome"]["draft"] = draft.model_dump(mode="json")
    payload["outcome"]["claims"] = {
        "claims": [
            {
                "claim_id": "date",
                "text": "Дата и место",
                "material": True,
                "citations": [{"source_id": doc.source_id, "url": doc.url, "quote": doc.body}],
            }
        ]
    }
    payload["outcome"]["verification"] = {
        "claims": [{"claim_id": "date", "supported": True, "confidence": 0.99, "reason": "Checked"}]
    }
    if attested:
        payload["presentation"] = {
            "profile_sha256": "0" * 64,
            "rubric_id": "weekend",
            "rubric_name": "Планы",
            "illustration": {},
            "event_place": resolve_saved_event_place(session, doc, [item.id], profile=PROFILE),
        }
    for review in payload["reviews"]:
        review.update(
            source_id=doc.source_id,
            article_id="concert",
            reviewed_at=NOW.isoformat(),
            draft_sha256=draft_sha256(draft),
        )
    post = Post(
        city="belgrade",
        category="events",
        title=draft.title,
        body=draft.body,
        status="ready",
        confidence=1,
        published_at=NOW,
        telegram_message_id=100,
    )
    session.add(post)
    session.flush()
    run = EditorialRun(
        raw_item_id=item.id,
        post_id=post.id,
        delivery_status="sent",
        outcome={},
        evidence={"private": "PRIVATE_INTAKE"},
    )
    persist_package(run, payload)
    session.add(run)
    session.commit()
    return item, run, post, payload


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_feed_only_exposes_allowlisted_published_fields_and_keeps_archive_id(session):
    item, run, post, payload = seed(session)
    first = export_events(session, observed_at=NOW)
    assert len(first.events) == 1
    event = first.events[0]
    assert event.post_url == "https://t.me/eduvbelgrad/100"
    assert event.ends_at is None and not event.archived
    later = export_events(session, observed_at=NOW + timedelta(days=20))
    assert later.events[0].archived and later.events[0].id == event.id
    assert "PRIVATE_INTAKE" not in first.model_dump_json()
    assert "reviews" not in first.model_dump_json() and "raw_item_id" not in first.model_dump_json()
    assert not session.dirty and not session.new and not session.deleted


def test_published_cukarica_event_without_html_uses_corroborated_program(session, monkeypatch):
    article = document().model_copy(update={
        "source_id": "cukarica_news",
        "url": _CUKARICA_POST_2026,
        "title": "Кабаре „Да, то су били дани“",
        "body": (
            "Кабаре ће бити изведено на великој сцени Културног центра „Чукарица“ "
            "24. новембра, са почетком у 20 часова."
        ),
    })
    item, _, post, _ = seed(session, doc=article, attested=False)
    session.delete(session.get(RawItemMediaSnapshot, item.id))
    post.telegram_message_id = 104
    session.commit()

    feed = export_events(session, observed_at=NOW)
    assert len(feed.events) == 1
    event = feed.events[0]
    assert event.post_url == "https://t.me/eduvbelgrad/104"
    assert event.starts_at.isoformat() == "2026-11-24T20:00:00+01:00"
    assert event.venue.address == "Turgenjevljeva 5, Beograd"
    monkeypatch.setattr(
        "relocant_media.ops.map_backfill.export_events",
        lambda session, observed_at: export_events(session, observed_at=NOW),
    )
    app = "https://t.me/relocant_media_belgrade_bot"
    assert plan_event_buttons(session, {104: event.id}, feed, app) == {
        104: f"{app}?startapp=places_{event.id}",
    }
    with pytest.raises(ValueError, match="confirmed afisha card"):
        plan_event_buttons(session, {103: event.id}, feed, app)


@pytest.mark.parametrize("verified_time", [True, False])
def test_legacy_concert_requires_grounded_published_time_without_saved_html(session, verified_time):
    article = _source(_DOB_INTERVIEW_2026)
    body = (
        "Publika će 16. novembra od 20 časova u Domu omladine Beograda "
        "imati priliku da doživi El Sonido de la Vida."
    )
    if not verified_time:
        body = body.replace("20 časova", "18 časova")
    article = article.model_copy(update={"body": body})
    item, _, _, _ = seed(session, doc=article, attested=False)
    session.delete(session.get(RawItemMediaSnapshot, item.id))
    session.commit()

    assert resolve_saved_event_place(session, article, [item.id], profile=PROFILE) is None
    feed = export_events(session, observed_at=NOW)
    if verified_time:
        assert len(feed.events) == 1 and feed.events[0].starts_at.hour == 20
        assert feed.events[0].schedule_source_url
    else:
        assert feed.events == ()


@pytest.mark.parametrize(
    "mutation",
    [
        "pending",
        "uncertain",
        "private_source",
        "other_city",
        "no_receipt",
        "changed_post",
        "changed_html",
        "changed_raw",
        "changed_hash",
        "unreviewed_place",
        "not_live",
        "other_host",
    ],
)
def test_unpublished_private_unbound_or_changed_material_is_excluded(session, mutation):
    item, run, post, payload = seed(session)
    if mutation in {"pending", "uncertain"}:
        run.delivery_status = mutation
    elif mutation == "private_source":
        session.get(Source, item.source_id).ownership = "unknown"
    elif mutation == "other_city":
        session.get(Source, item.source_id).city = "novi_sad"
    elif mutation == "no_receipt":
        post.telegram_message_id = None
    elif mutation == "changed_post":
        post.body += " изменено после ревью"
    elif mutation == "changed_html":
        session.get(RawItemMediaSnapshot, item.id).source_html += "changed"
    elif mutation == "changed_raw":
        item.body += "changed"
    elif mutation == "changed_hash":
        run.outcome = {**run.outcome, "_package_sha256": "0" * 64}
    elif mutation == "unreviewed_place":
        payload["presentation"]["event_place"]["venue"]["latitude"] = 40
        persist_package(run, payload)
    elif mutation == "not_live":
        payload["reviews"][0]["live_source_checked"] = False
        persist_package(run, payload)
    elif mutation == "other_host":
        session.get(Source, item.source_id).url = "https://other.example/"
    session.commit()
    assert export_events(session, observed_at=NOW).events == ()


def test_legacy_exact_supported_date_and_venue_can_be_backfilled(session):
    seed(session, attested=False)
    assert len(export_events(session, observed_at=NOW).events) == 1


@pytest.mark.parametrize("mutation", ["missing_time", "unsupported", "not_in_draft"])
def test_legacy_backfill_requires_supported_and_used_time_claims(session, mutation):
    item, run, post, payload = seed(session, attested=False)
    if mutation == "missing_time":
        payload["outcome"]["claims"]["claims"][0]["citations"][0]["quote"] = "Mali koncert"
    elif mutation == "unsupported":
        payload["outcome"]["verification"]["claims"][0]["supported"] = False
    else:
        payload["outcome"]["draft"]["claim_ids"] = ["other"]
    for review in payload["reviews"]:
        review["draft_sha256"] = draft_sha256(
            RussianDraft.model_validate(payload["outcome"]["draft"])
        )
    persist_package(run, payload)
    session.commit()
    assert export_events(session, observed_at=NOW).events == ()


def test_legacy_dob_article_uses_exact_linked_card_year_and_room(session):
    from test_event_place_adapters import _INTERVIEW, _interview, _page, _slide, _source

    doc = _source().model_copy(
        update={
            "body": (
                "Publika će 16. novembra od 20 časova u Domu omladine Beograda doživeti "
                "El Sonido de la Vida – Zvuk života."
            )
        }
    )
    seed(session, doc=doc, source_html=_page(_INTERVIEW, _interview(), _slide()), attested=False)
    feed = export_events(session, observed_at=NOW)
    assert len(feed.events) == 1
    assert feed.events[0].starts_at.isoformat() == "2026-11-16T20:00:00+01:00"
    assert feed.events[0].venue.name.endswith(" · Amerikana")
    assert feed.events[0].coordinate_source_url.endswith("/way/41234985")


@pytest.mark.parametrize("valid", [True, False])
def test_owner_path_requires_matching_published_owner_receipt(session, valid):
    item, run, post, payload = seed(session)
    payload["outcome"]["status"] = "human_review"
    persist_package(run, payload)
    receipt = {
        "raw_item_id": item.id,
        "package_sha256": run.outcome["_package_sha256"],
        "draft_sha256": payload["reviews"][0]["draft_sha256"],
        "telegram_channel_message_id": 100 if valid else 101,
        "published_at": NOW.isoformat(),
        "private_comment": "SECRET",
    }
    session.add(Submission(kind="owner_editorial_review", status="published", payload=receipt))
    session.commit()
    assert len(export_events(session, observed_at=NOW).events) == int(valid)


def test_readonly_snapshot_and_pages_copy_preserve_feed(tmp_path):
    database = tmp_path / "editorial database.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
    engine.dispose()
    before = database.read_bytes()
    target = tmp_path / "site/map/events.json"
    export_events_snapshot(database, target, NOW.isoformat())
    assert database.read_bytes() == before
    content = target.read_bytes()
    assert len(read_events_feed(content).events) == 1
    copy_assets(Path.cwd(), target.parent.parent)
    assert target.read_bytes() == content


def test_missing_local_feed_restores_valid_remote_without_overwriting(tmp_path):
    calls = []
    payload = {"version": 1, "events": []}

    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith(EVENTS_PATH)
        return httpx.Response(
            200, json=payload if "raw" in request.headers["Accept"] else {"sha": "a"}
        )

    path = tmp_path / "events.json"
    with httpx.Client(
        base_url="https://example.org/", transport=httpx.MockTransport(handler)
    ) as client:
        _publish_events(client, path)
    assert len(calls) == 2 and read_events_feed(path.read_bytes()).events == ()


def test_public_schema_rejects_extra_fields_and_missing_timezone():
    with pytest.raises(ValueError):
        read_events_feed(json.dumps({"version": 1, "events": [], "private": "SECRET"}).encode())
    with pytest.raises(ValueError):
        read_events_feed(b'{"version":1,"events":[],"generated_at":"2026-11-13T10:00:00"}')

def test_cafe_appears_only_inside_approved_paid_map_campaign():
    from relocant_media.ops.events_export import _sponsored_places

    row = {
        "channel_message_id": 112, "starts_at": "2026-11-14T07:00:00+01:00",
        "ends_at": "2026-11-17T20:00:00+01:00", "paid": True,
        "owner_approved": True, "map_featured": True,
        "place": {
            "title": "Кафе", "source_url": "https://example.org/cafe",
            "venue": {"name": "Кафе", "address": "Белград, 12",
                      "latitude": 44.81, "longitude": 20.46},
        },
    }
    now = datetime(2026, 11, 15, 8, tzinfo=UTC)
    zone = "Europe/Belgrade"
    unpaid = {"advertisements": [{**row, "paid": False}]}
    assert not _sponsored_places(unpaid, "belgrade", zone, now)
    cards = _sponsored_places({"advertisements": [row]}, "belgrade", zone, now)
    assert len(cards) == 1 and cards[0].kind == "sponsored_place"
    assert cards[0].post_url == "https://t.me/eduvbelgrad/112"
    end = datetime.fromisoformat(row["ends_at"])
    assert not _sponsored_places({"advertisements": [row]}, "belgrade", zone, end)
