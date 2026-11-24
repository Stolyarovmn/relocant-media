import copy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from facts_check_helpers import bind_facts_check
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_events_export import persist_package, seed

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import EditorialPackage, canonical_json, draft_sha256
from relocant_media.collectors.cukarica_news import parse_cukarica_article
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import event_key
from relocant_media.domain.map_links import map_event_id
from relocant_media.ops.events_export import export_events, read_events_feed
from relocant_media.ops.map_backfill import plan_event_buttons
from relocant_media.ops.server_cycle import policy_reassessment_ids
from relocant_media.services.editorial import _candidate, _snapshot_member
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    verify_live_map,
)
from relocant_media.services.event_place import resolve_event_place
from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.services.policy_reassessment import eligible_map_hold
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, Source

URL = "https://www.cukarica.rs/vest/711-dvanaesti-cukafest-od-2-do-7-decembra/"
PROFILE = load_city_profile("belgrade")
NOW = datetime(2026, 11, 19, 0, tzinfo=UTC)


def article(page=None):
    page = page or Path("tests/fixtures/cukafest.html").read_text(encoding="utf-8")
    item = parse_cukarica_article(
        page, load_source_file(Path("cities/belgrade/sources/cukarica_news.yaml")), URL,
    )
    return EvidenceDocument.model_validate(item, from_attributes=True), page


def test_program_preserves_six_sessions_at_five_places_with_independent_expiry():
    doc, page = article()
    place = resolve_event_place(doc, page, profile=PROFILE)
    assert place["id"] == map_event_id(URL)
    slots = place["occurrences"]
    assert len(slots) == len({s["id"] for s in slots}) == 6
    assert len({s["venue"]["name"] for s in slots}) == 5
    assert [s["starts_at"] for s in slots] == [
        "2026-12-02T21:00:00+01:00", "2026-12-03T20:30:00+01:00",
        "2026-12-04T20:00:00+01:00", "2026-12-05T20:00:00+01:00",
        "2026-12-06T18:00:00+01:00", "2026-12-07T18:00:00+01:00",
    ]
    assert all(s["ends_at"] is None and s["group_id"] == place["id"] for s in slots)
    assert slots[0]["expires_at"] == "2026-12-03T00:00:00+01:00"
    assert place["expires_at"] == "2026-12-08T00:00:00+01:00"
    assert slots[1]["venue"]["latitude"] == 44.788216
    assert "5N" not in slots[1]["venue"]["address"]
    assert "5N" in slots[1]["venue_note"]


@pytest.mark.parametrize("before,after", [
    ("Vox Blues Clubu", "Nepoznata sala"),
    ("у малој сали", "у великој сали"),
    ("од 20.30 часова", "увече"),
    ("Седмог децембра", "Осмог децембра"),
    ("Улаз на концерте је слободан.", "Фестивал је отказан."),
    ("на пет локација", "на шест локација"),
])
def test_incomplete_or_changed_program_does_not_silently_publish_a_partial_map(before, after):
    _, page = article()
    # Последний день исходник называет «завршног», нормализуем для проверки новой даты.
    if before == "Седмог децембра":
        page = page.replace("Завршног дана фестивала", "Седмог децембра")
    assert before in page
    doc, changed = article(page.replace(before, after))
    assert resolve_event_place(doc, changed, profile=PROFILE) is None


@pytest.fixture
def state():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        doc, page = article()
        item, run, post, payload = seed(session, doc=doc, source_html=page)
        source = session.get(Source, item.source_id)
        source.auto_publish = True
        session.commit()
        payload["event"].update(event_key=event_key(_candidate(item)),
                                documents=[_snapshot_member(item, source)])
        documents = (EvidenceDocument.model_validate(item, from_attributes=True),)
        presentation = build_presentation(session, item.id, documents, document_ids=[item.id],
                                          profile=PROFILE, rubric_id="weekend")
        draft = EditorialPackage.model_validate_json(canonical_json(payload)).outcome.draft
        draft = draft.model_copy(update={"title": "«Чукафест» 2–7 декабря на пяти площадках"})
        draft = present_draft(draft, presentation, category="events")
        payload["outcome"]["draft"] = draft.model_dump(mode="json")
        payload["outcome"]["ranking"]["selection"] = {
            "policy_version": PROFILE.policy_version, "profile_sha256": PROFILE.sha256,
            "audience": "Жители Белграда", "benefit": "Выбрать бесплатный концерт",
            "novelty": "Опубликована программа фестиваля", "why_now": "Событие впереди",
            "rubric_id": "weekend", "locality": "city", "value_kind": "action",
            "evidence_refs": [{"source_id": doc.source_id, "url": doc.url,
                               "quote": doc.body[:500]}],
        }
        payload["presentation"] = presentation.model_dump(mode="json")
        bind_facts_check(payload)
        for review in payload["reviews"]:
            review["reviewed_at"] = NOW.isoformat()
            review["draft_sha256"] = draft_sha256(draft)
        post.title, post.body = draft.title, draft.body
        post.published_at = NOW
        run.evidence = {**documents[0].model_dump(mode="json"), "city": "belgrade",
                        "event_key": payload["event"]["event_key"],
                        "documents": payload["event"]["documents"]}
        persist_package(run, payload)
        session.commit()
        yield engine, session, item, run, post, payload
    engine.dispose()


def test_published_program_exports_all_slots_and_hides_only_finished_dates(state):
    _, session, _, _, _, _ = state
    feed = export_events(session, observed_at=NOW)
    assert len(feed.events) == 6
    assert {event.group_id for event in feed.events} == {map_event_id(URL)}
    assert len({event.venue.name for event in feed.events}) == 5
    assert read_events_feed(feed.model_dump_json().encode()) == feed
    later = export_events(session, observed_at=datetime(2026, 12, 5, 0, tzinfo=UTC))
    assert sum(event.archived for event in later.events) == 3
    assert len({event.id for event in later.events}) == 6


def test_live_program_tolerates_changed_page_wrapper_without_relaxing_review_hashes(state):
    _, _, _, _, _, payload = state
    package = EditorialPackage.model_validate_json(canonical_json(payload))
    doc, page = article()
    fresh = page + "<!-- изменился служебный счётчик страницы -->"
    assert resolve_event_place(doc, page, profile=PROFILE) != resolve_event_place(
        doc, fresh, profile=PROFILE,
    )
    verify_live_map(package.presentation, doc, fresh)


@pytest.mark.parametrize("change", ["coordinate", "time", "quote", "missing_slot"])
def test_live_program_still_blocks_any_changed_child_fact(state, change):
    _, _, _, _, _, payload = state
    package = EditorialPackage.model_validate_json(canonical_json(payload))
    place = copy.deepcopy(package.presentation.event_place)
    if change == "coordinate":
        place["occurrences"][1]["venue"]["latitude"] += 0.01
    elif change == "time":
        place["occurrences"][2]["starts_at"] = "2026-12-04T22:00:00+01:00"
    elif change == "quote":
        place["occurrences"][0]["source_quote"] = "Другая программа"
    else:
        place["occurrences"].pop()
    doc, page = article()
    with pytest.raises(ValueError, match="Площадка или расписание"):
        verify_live_map(package.presentation.model_copy(update={"event_place": place}), doc, page)


def test_group_button_requires_complete_matching_public_feed(state, monkeypatch):
    from types import SimpleNamespace

    from relocant_media.ops import map_backfill

    monkeypatch.setattr(map_backfill, "datetime", SimpleNamespace(now=lambda _: NOW))
    _, session, _, _, _, _ = state
    feed = export_events(session, observed_at=NOW)
    buttons = plan_event_buttons(session, {100: map_event_id(URL)}, feed,
                                "https://t.me/relocant_media_belgrade_bot/map")
    assert buttons[100].endswith("startapp=places_" + map_event_id(URL))
    with pytest.raises(ValueError, match="confirmed afisha"):
        plan_event_buttons(session, {100: map_event_id(URL)},
                           feed.model_copy(update={"events": feed.events[:-1]}),
                           "https://t.me/relocant_media_belgrade_bot/map")


def hold(state):
    engine, session, item, run, post, payload = state
    old = copy.deepcopy(payload)
    old["presentation"].pop("event_place")
    post.published_at = post.telegram_message_id = None
    run.delivery_status = "pending"
    persist_package(run, old)
    run.outcome = {**run.outcome, "status": "human_review",
                   "reason": "event_place_requires_verification"}
    session.commit()
    return engine, session, item, run, post, payload


def test_completed_map_reenters_pipeline_and_preserves_previous_package_in_audit(state):
    engine, session, item, run, _, payload = hold(state)
    assert eligible_map_hold(session, item.id, now=NOW)
    assert item.id in policy_reassessment_ids(engine, now=NOW)[0]
    expired = datetime(2026, 11, 25, 0, tzinfo=UTC)
    assert item.id not in policy_reassessment_ids(engine, now=expired)[0]
    with pytest.raises(PackageConflictError):
        import_package(session, EditorialPackage.model_validate_json(canonical_json(payload)),
                       now=NOW)
    previous = copy.deepcopy(run.outcome)
    # persist_package перепривязал хеши в payload только при исходном сохранении.
    from test_editorial_package import bind_context

    bind_context(payload)
    result = import_package(session, EditorialPackage.model_validate_json(canonical_json(payload)),
                            now=NOW, replace_map_hold=True)
    assert result.outcome["status"] == "ready"
    assert result.outcome["_previous_policy_hold"]["outcome"] == previous
    assert result.delivery_status == "pending"
    assert not eligible_map_hold(session, item.id, now=NOW)


@pytest.mark.parametrize("change", ["sending", "uncertain", "sent", "published", "receipt",
                                    "draft", "evidence", "disabled", "no_map"])
def test_recovery_never_overwrites_delivery_or_changed_evidence(state, change):
    _, session, item, run, post, _ = hold(state)
    if change in {"sending", "uncertain", "sent"}:
        run.delivery_status = change
    elif change == "published":
        post.telegram_message_id = 333
    elif change == "receipt":
        run.outcome = {**run.outcome, "_rich_delivery": {"state": "uncertain"}}
    elif change == "draft":
        post.body += " изменено"
    elif change == "evidence":
        item.body += " изменено"
    elif change == "disabled":
        session.get(Source, item.source_id).enabled = False
    else:
        snapshot = session.get(RawItemMediaSnapshot, item.id)
        snapshot.source_html = None
    session.commit()
    assert not eligible_map_hold(session, item.id, now=NOW)
