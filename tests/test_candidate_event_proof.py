"""Обычный selector использует доказательство события, а не сам факт старого отказа."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from test_material_duplicate_review import outcome

from relocant_media.domain.policy import SOURCE_REPLACED_REASON
from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.services import material_lifecycle as core
from relocant_media.services.editorial import EditorialService, _document, _snapshot_member
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
BODY = "Beograd obaveštava putnike o promenama u radu lokalnog prevoza."


@pytest.fixture
def engine(tmp_path, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)

    monkeypatch.setattr("relocant_media.services.editorial.datetime", FixedDatetime)
    path = tmp_path / "selector.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        session.add(Source(id="official", city="belgrade", name="Синтетический источник",
            url="https://example.test", source_type="official_news", trust=1,
            ownership="official", enabled=True, auto_publish=True, high_risk=False))
        session.flush()
        session.add_all(raw(raw_id) for raw_id in (1, 2))
        session.commit()
    yield engine
    engine.dispose()


def raw(raw_id, *, url=None):
    return RawItem(id=raw_id, source_id="official", external_id=str(raw_id),
        url=url or f"https://example.test/{raw_id}", title="Городское предупреждение пассажирам",
        body=BODY, published_at=NOW-timedelta(minutes=5), published_at_precision="instant",
        ingested_at=NOW)


def select_fresh(session, **options):
    return select_candidates(session, city="belgrade", since=NOW-timedelta(hours=1),
                             until=NOW, limit=5, **options)


def store_decision(engine, kind):
    with Session(engine) as session:
        item, source = session.get(RawItem, 1), session.get(Source, "official")
        if kind in {"skipped", "owner_closed", "replacement"}:
            decision = {"status": "skipped", "reason": "not_selected"}
            if kind == "replacement":
                decision = {"status": "skipped", "reason": SOURCE_REPLACED_REASON,
                            "replacement_raw_item_id": 2}
            session.add(EditorialRun(raw_item_id=1, outcome=decision, evidence={},
                                     delivery_status="pending", created_at=NOW))
            session.flush()
            if kind == "owner_closed":
                core.close_by_owner(session, 1, now=NOW)
        else:
            snapshot = _snapshot_member(item, source)
            decision = outcome(_document(item))
            post = Post(city="belgrade", category="transport", status="ready",
                title=decision.draft.title, body=decision.draft.body, confidence=0.95)
            session.add(post)
            session.flush()
            record = EditorialRun(raw_item_id=1, post_id=post.id,
                outcome=decision.model_dump(mode="json"),
                evidence={**snapshot, "city": "belgrade", "documents": [snapshot]},
                delivery_status="pending", created_at=NOW)
            if kind == "published":
                record.delivery_status, post.telegram_message_id = "sent", 321
            elif kind in {"sending", "uncertain"}:
                record.delivery_status = kind
            elif kind == "staging":
                record.staging_message_id = 42
            elif kind == "sent_no_receipt":
                record.delivery_status = "sent"
            session.add(record)
        session.commit()


@pytest.mark.parametrize("kind", ["skipped", "owner_closed", "replacement"])
@pytest.mark.parametrize("same_url", [False, True])
def test_normal_cycle_reaches_full_processing_after_old_nonproof(engine, tmp_path, kind, same_url):
    store_decision(engine, kind)
    if same_url:
        with Session(engine) as session:
            session.get(RawItem, 2).url = session.get(RawItem, 1).url
            session.commit()
    calls = []

    class FullPipeline:
        def run(self, *, city, documents):
            calls.append((city, documents))
            return outcome(documents[0])

    def process(raw_id):
        with Session(engine) as session:
            record = EditorialService(session, FullPipeline()).prepare(raw_id)
            return {"raw_item_id": raw_id, "status": record.outcome["status"]}

    result = run_cycle(engine, city="belgrade", now=NOW, output=tmp_path / "normal",
        ingest=lambda: {"failures": 0}, process=process, allowed_items=lambda: {2})
    assert result["processed_raw_item_ids"] == [2]
    assert len(calls) == 1 and calls[0][0] == "belgrade"
    with Session(engine) as session:
        stored = session.get(EditorialRun, 2)
        assert stored.post_id is not None and stored.outcome["reason"] != "event_already_reviewed"
        assert all(stored.outcome[key] for key in ("claims", "verification", "draft"))
        assert stored.delivery_status == "pending"
        if kind == "owner_closed":
            assert core.is_terminal_blocked(session, 1, now=NOW)


@pytest.mark.parametrize("kind", ["reviewed", "published", "sending", "uncertain", "staging",
                                  "sent_no_receipt"])
def test_proven_review_or_transport_still_suppresses_twin_without_writes(engine, kind):
    store_decision(engine, kind)
    with Session(engine) as session:
        old = session.get(EditorialRun, 1)
        receipt = old.delivery_status, old.staging_message_id
        session.execute(text("PRAGMA query_only=ON"))
        result = select_fresh(session)
        assert result["eligible_count"] == 0
        assert not session.new and not session.dirty
        assert (old.delivery_status, old.staging_message_id) == receipt


def test_selector_keeps_own_run_scope_and_explicit_exclusions(engine):
    store_decision(engine, "skipped")
    with Session(engine) as session:
        assert [row["raw_item_id"] for row in select_fresh(session)["candidates"]] == [2]
        assert select_fresh(session, allowed_raw_ids={1})["eligible_count"] == 0
        assert select_fresh(session, excluded_raw_ids={2})["eligible_count"] == 0
        session.add(EditorialRun(raw_item_id=2, outcome={"status": "skipped"}, evidence={},
                                 delivery_status="pending"))
        session.commit()
        assert select_fresh(session)["eligible_count"] == 0


@pytest.mark.parametrize("kind", ["published", "sending", "uncertain"])
def test_saved_transport_snapshot_suppresses_twin_after_current_raw_changes(engine, kind):
    store_decision(engine, kind)
    with Session(engine) as session:
        old = session.get(RawItem, 1)
        old.url, old.title, old.body = ("https://example.test/changed", "Другой сюжет",
                                       "Совершенно другие сведения о другом событии.")
        old.published_at = NOW-timedelta(days=30)
        session.commit()
    with Session(engine) as session:
        session.execute(text("PRAGMA query_only=ON"))
        assert select_fresh(session)["eligible_count"] == 0
        assert not session.new and not session.dirty


def test_legacy_receipt_selection_does_not_import_runtime_ai_or_telegram(engine, monkeypatch):
    import builtins

    store_decision(engine, "published")
    with Session(engine) as session:
        session.get(EditorialRun, 1).evidence = {}
        session.commit()
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(("relocant_media.ai", "relocant_media.bot", "relocant_media.config")):
            pytest.fail("Read-only отбор не должен загружать AI/runtime/Telegram")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with Session(engine) as session:
        session.execute(text("PRAGMA query_only=ON"))
        assert select_fresh(session)["eligible_count"] == 0


def test_archive_of_skipped_bodies_is_not_loaded_for_each_candidate(engine):
    store_decision(engine, "skipped")
    with Session(engine) as session:
        session.add_all(raw(raw_id) for raw_id in range(100, 1600))
        session.flush()
        session.add_all(EditorialRun(raw_item_id=raw_id, outcome={"status": "skipped"},
            evidence={}, delivery_status="pending") for raw_id in range(100, 1600))
        session.commit()
    loaded = []

    def on_load(item, context):
        loaded.append(item.id)

    event.listen(RawItem, "load", on_load)
    try:
        with Session(engine) as session:
            session.execute(text("PRAGMA query_only=ON"))
            assert select_fresh(session)["eligible_count"] == 1
            assert set(loaded) == {2}
    finally:
        event.remove(RawItem, "load", on_load)
