from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.services.event_lifecycle import source_revision_lifecycle_context
from relocant_media.services.newsroom_control import build_newsroom_inbox
from relocant_media.services.source_revision_control import (
    build_source_revision_control,
    record_source_revision_decision,
)
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import (
    SourceRevisionDecision,
    SourceRevisionMaterialization,
)


def _seed(session: Session) -> tuple[RawItem, SourceRevision, EditorialRun]:
    source = Source(
        id="official",
        city="belgrade",
        name="Official",
        url="https://example.test",
        source_type="official",
        trust=Decimal("1"),
        ownership="official",
        high_risk=False,
        auto_publish=True,
        enabled=True,
    )
    session.add(source)
    session.flush()
    published_at = datetime(2026, 10, 21, 10, tzinfo=UTC)
    raw = RawItem(
        source_id=source.id,
        external_id="event-1",
        url="https://example.test/event-1",
        canonical_url="https://example.test/event-1",
        content_sha256="a" * 64,
        title="Event",
        body="Original evidence",
        published_at=published_at,
    )
    session.add(raw)
    session.flush()
    run = EditorialRun(
        raw_item_id=raw.id,
        outcome={"status": "ready", "reason": "ready"},
        evidence={"raw_item_id": raw.id},
        delivery_status="pending",
        created_at=datetime(2026, 10, 21, 11, tzinfo=UTC),
    )
    revision = SourceRevision(
        raw_item_id=raw.id,
        snapshot_sha256="b" * 64,
        evidence={
            "source_id": source.id,
            "article_id": raw.external_id,
            "url": raw.url,
            "title": "Event — updated",
            "body": "Changed evidence with a new venue and time.",
            "published_at": published_at.isoformat(),
        },
        observed_at=datetime(2026, 10, 21, 12, tzinfo=UTC),
    )
    session.add_all([run, revision])
    session.flush()
    return raw, revision, run


def test_queue_reverify_materializes_fresh_candidate_idempotently() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw, revision, run = _seed(session)
        first = record_source_revision_decision(
            session,
            revision_id=revision.id,
            decision="queue_reverify",
            reason_code="material_source_change",
            actor="editorial-agent",
            idempotency_key="revision-1-reverify",
            note="Fresh review required",
            decided_at=datetime(2026, 10, 21, 12, 5, tzinfo=UTC),
        )
        repeated = record_source_revision_decision(
            session,
            revision_id=revision.id,
            decision="queue_reverify",
            reason_code="material_source_change",
            actor="editorial-agent",
            idempotency_key="revision-1-reverify",
            note="Fresh review required",
            decided_at=datetime(2026, 10, 21, 12, 10, tzinfo=UTC),
        )
        assert repeated.id == first.id
        assert run.delivery_status == "pending"
        assert run.outcome == {"status": "ready", "reason": "ready"}
        assert session.query(SourceRevisionDecision).count() == 1
        assert session.query(SourceRevisionMaterialization).count() == 1
        assert session.query(RawItem).count() == 2

        materialization = session.scalar(select(SourceRevisionMaterialization))
        assert materialization is not None
        candidate = session.get(RawItem, materialization.raw_item_id)
        assert candidate is not None
        assert materialization.parent_raw_item_id == raw.id
        assert candidate.source_id == raw.source_id
        assert candidate.url == raw.url
        assert candidate.external_id == f"event-1::revision:{revision.id}"
        assert candidate.title == "Event — updated"
        assert "__source_revision=" in (candidate.canonical_url or "")

        lifecycle = source_revision_lifecycle_context(session, candidate.id)
        assert lifecycle is not None
        assert lifecycle["kind"] == "source_update"
        assert lifecycle["parent_raw_item_id"] == raw.id
        assert lifecycle["raw_item_id"] == candidate.id
        assert lifecycle["parent_event_key"] != lifecycle["event_key"]
        assert lifecycle["fresh_editorial_review_created"] is False

        inbox = build_newsroom_inbox(session, city="belgrade")
        item = next(row for row in inbox["items"] if raw.id in row["raw_item_ids"])
        assert item["state"] == "needs_reverify"
        assert item["reason_code"] == "source_revision_after_review"
    engine.dispose()


def test_non_queue_decision_does_not_materialize_revision() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _raw, revision, _run = _seed(session)
        record_source_revision_decision(
            session,
            revision_id=revision.id,
            decision="hold",
            reason_code="needs_context",
            actor="operator",
            idempotency_key="hold-key",
        )
        assert session.query(SourceRevisionMaterialization).count() == 0
        assert session.query(RawItem).count() == 1
    engine.dispose()


def test_idempotency_key_cannot_be_reused_for_different_payload() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _raw, revision, _run = _seed(session)
        record_source_revision_decision(
            session,
            revision_id=revision.id,
            decision="hold",
            reason_code="needs_context",
            actor="operator",
            idempotency_key="same-key",
        )
        with pytest.raises(ValueError, match="another decision"):
            record_source_revision_decision(
                session,
                revision_id=revision.id,
                decision="superseded",
                reason_code="newer_revision_exists",
                actor="operator",
                idempotency_key="same-key",
            )
    engine.dispose()


def test_invalid_decision_is_rejected() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _raw, revision, _run = _seed(session)
        with pytest.raises(ValueError, match="unsupported"):
            record_source_revision_decision(
                session,
                revision_id=revision.id,
                decision="approve_and_publish",
                reason_code="unsafe",
                actor="operator",
                idempotency_key="unsafe-decision",
            )
    engine.dispose()


def test_control_projection_prioritizes_undecided_then_newest() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw, first, _run = _seed(session)
        second = SourceRevision(
            raw_item_id=raw.id,
            snapshot_sha256="c" * 64,
            evidence={
                **first.evidence,
                "title": "Event — newest",
                "body": "Newest evidence",
            },
            observed_at=first.observed_at + timedelta(hours=1),
        )
        session.add(second)
        session.flush()
        record_source_revision_decision(
            session,
            revision_id=first.id,
            decision="hold",
            reason_code="awaiting_context",
            actor="operator",
            idempotency_key="first-hold",
        )

        report = build_source_revision_control(session, city="belgrade")
        assert report["version"] == 2
        assert report["undecided_revisions"] == 1
        assert report["items"][0]["revision_id"] == second.id
        assert report["items"][0]["decision_status"] == "undecided"
        assert report["items"][1]["latest_decision"]["decision"] == "hold"
        assert report["items"][1]["decision_does_not_clear_fresh_review"] is True
        assert report["items"][1]["fresh_review_candidate_created"] is False
        assert report["safety"]["decision_clears_reverification"] is False
        assert report["safety"]["queue_reverify_reuses_old_editorial_run"] is False
    engine.dispose()
