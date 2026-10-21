from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_editorial_pipeline import FakeProvider, _draft, _ranking, _verification

from relocant_media.ai.models import ClaimSet, EvidenceCitation, ExtractedClaim
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.services.editorial import EditorialService
from relocant_media.services.event_lifecycle import source_revision_lifecycle_context
from relocant_media.services.source_revision_control import record_source_revision_decision
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import SourceRevisionMaterialization


def _seed_reviewed_item(session: Session, *, delivery_status: str = "pending") -> RawItem:
    source = Source(
        id="official-lifecycle",
        city="belgrade",
        name="Official lifecycle source",
        url="https://official.example",
        source_type="official",
        trust=Decimal("1"),
        ownership="official",
        high_risk=False,
        auto_publish=True,
        enabled=True,
    )
    session.add(source)
    session.flush()
    item = RawItem(
        source_id=source.id,
        external_id="event-1",
        url="https://official.example/events/1",
        canonical_url="https://official.example/events/1",
        content_sha256="a" * 64,
        title="Festival 25 October",
        body="Festival starts at 18:00 at Old Hall.",
        published_at=datetime(2026, 10, 20, 10, tzinfo=UTC),
    )
    session.add(item)
    session.flush()
    session.add(
        EditorialRun(
            raw_item_id=item.id,
            outcome={"status": "ready", "reason": "ready"},
            evidence={"raw_item_id": item.id},
            delivery_status=delivery_status,
        )
    )
    session.flush()
    return item


def test_queued_source_update_receives_fresh_full_editorial_review() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        parent = _seed_reviewed_item(session)
        revision = SourceRevision(
            raw_item_id=parent.id,
            snapshot_sha256="b" * 64,
            evidence={
                "source_id": parent.source_id,
                "article_id": parent.external_id,
                "url": parent.url,
                "title": "Festival moved to 26 October",
                "body": "Festival now starts at 19:00 at New Hall on 26 October 2026.",
                "published_at": datetime(2026, 10, 21, 8, tzinfo=UTC).isoformat(),
            },
        )
        session.add(revision)
        session.flush()
        record_source_revision_decision(
            session,
            revision_id=revision.id,
            decision="queue_reverify",
            reason_code="official_event_changed",
            actor="editorial-agent",
            idempotency_key="event-lifecycle-reverify",
            expected_decision_count=0,
        )
        materialization = session.scalar(select(SourceRevisionMaterialization))
        assert materialization is not None
        candidate = session.get(RawItem, materialization.raw_item_id)
        assert candidate is not None

        claims = ClaimSet(
            claims=(
                ExtractedClaim(
                    claim_id="c1",
                    text=candidate.body,
                    citations=(
                        EvidenceCitation(
                            source_id=candidate.source_id,
                            url=candidate.url,
                            quote=candidate.body,
                        ),
                    ),
                ),
            )
        )
        provider = FakeProvider(
            {
                "rank": _ranking("events"),
                "claims": claims,
                "verify": _verification(),
                "draft": _draft(),
            }
        )
        service = EditorialService(session, EditorialPipeline(provider))
        run = service.prepare(candidate.id)

        assert run.raw_item_id == candidate.id
        assert run.raw_item_id != parent.id
        assert provider.calls == ["rank", "claims", "verify", "draft"]
        assert session.get(EditorialRun, parent.id) is not None
        lifecycle = source_revision_lifecycle_context(session, candidate.id)
        assert lifecycle is not None
        assert lifecycle["fresh_editorial_review_created"] is True
        assert lifecycle["parent_raw_item_id"] == parent.id
        assert lifecycle["event_key"] != lifecycle["parent_event_key"]
    engine.dispose()


def test_changed_source_does_not_rewrite_already_sent_editorial_history() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        parent = _seed_reviewed_item(session, delivery_status="sent")
        original_run = session.get(EditorialRun, parent.id)
        assert original_run is not None
        original_outcome = dict(original_run.outcome)

        written, review_required = capture_collected_batch(
            session,
            [
                CollectedRawItem(
                    source_id=parent.source_id,
                    external_id=parent.external_id,
                    url=parent.url,
                    title="Festival moved to 26 October",
                    body="Festival now starts at 19:00 at New Hall on 26 October 2026.",
                    published_at=datetime(2026, 10, 21, 8, tzinfo=UTC),
                )
            ],
        )
        session.flush()

        assert written == 0
        assert review_required == 1
        session.refresh(original_run)
        assert original_run.delivery_status == "sent"
        assert original_run.outcome == original_outcome
        assert session.query(SourceRevision).count() == 1
    engine.dispose()
