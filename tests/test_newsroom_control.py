from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.services.newsroom_control import build_newsroom_inbox
from relocant_media.storage.models import (
    Base,
    CollectorFailure,
    EditorialRun,
    Post,
    RawItem,
    Source,
    SourceRevision,
    Submission,
)


def _engine() -> Engine:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _source(source_id: str, *, official: bool = True) -> Source:
    return Source(
        id=source_id,
        city="belgrade",
        name=source_id,
        url=f"https://{source_id}.example/",
        source_type="official_news",
        trust=Decimal("0.900"),
        ownership="official" if official else "independent",
        high_risk=False,
        auto_publish=True,
        enabled=True,
    )


def _raw(source_id: str, external_id: str, title: str, when: datetime) -> RawItem:
    return RawItem(
        source_id=source_id,
        external_id=external_id,
        url=f"https://{source_id}.example/{external_id}",
        canonical_url=f"https://{source_id}.example/{external_id}",
        content_sha256=(external_id * 64)[:64],
        title=title,
        body=f"Body for {title}",
        published_at=when,
        ingested_at=when,
    )


def _post(title: str) -> Post:
    return Post(
        city="belgrade",
        category="transport",
        status="ready",
        title=title,
        body=f"Draft for {title}",
        confidence=Decimal("0.9500"),
        editorial_rule_version="test-rule",
    )


def _run(
    raw: RawItem,
    post: Post,
    *,
    status: str = "ready",
    delivery: str = "pending",
) -> EditorialRun:
    created = raw.published_at + timedelta(minutes=5) if raw.published_at else datetime.now(UTC)
    return EditorialRun(
        raw_item_id=raw.id,
        outcome={"status": status, "reason": None},
        evidence={
            "city": "belgrade",
            "event_key": f"event-{raw.id}",
            "documents": [{"raw_item_id": raw.id}],
        },
        post_id=post.id,
        editorial_rule_version="test-rule",
        delivery_status=delivery,
        created_at=created,
    )


def test_inbox_is_read_only_projection_and_redacts_submission_payload() -> None:
    engine = _engine()
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("city"))
        raw = _raw("city", "a", "Fresh transport notice", now)
        session.add(raw)
        session.flush()
        session.add(
            Submission(
                kind="news_tip",
                telegram_user_id=123,
                telegram_message_id=456,
                payload={
                    "city": "belgrade",
                    "raw_item_ids": [raw.id],
                    "text": "private user text that must not leave the intake model",
                },
                status="source_linked",
                created_at=now,
            )
        )
        session.add(
            CollectorFailure(
                source_id="city",
                strategy="rss",
                error_type="TimeoutError",
                message="private upstream diagnostic",
                occurred_at=now,
            )
        )
        session.commit()

        before = session.query(EditorialRun).count()
        payload = build_newsroom_inbox(session, city="belgrade")
        after = session.query(EditorialRun).count()

    assert before == after == 0
    assert payload["counts"] == {"evidence_ready": 1}
    item = payload["items"][0]
    assert item["state"] == "evidence_ready"
    assert item["reason_code"] == "awaiting_editorial"
    assert item["submissions"][0]["kind"] == "news_tip"
    assert "payload" not in item["submissions"][0]
    assert "text" not in item["submissions"][0]
    assert item["last_error"] == {
        "source_id": "city",
        "strategy": "rss",
        "error_type": "TimeoutError",
        "occurred_at": now.isoformat(),
    }
    assert item["retry"] == {
        "automatic_retry": True,
        "policy": "scheduler_or_operator",
    }


def test_inbox_exposes_delivery_and_reverification_states() -> None:
    engine = _engine()
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("city"))
        raws = [
            _raw("city", "b", "Ready transport notice", now),
            _raw("city", "c", "Sent road notice", now + timedelta(minutes=10)),
            _raw("city", "d", "Uncertain bus notice", now + timedelta(minutes=20)),
            _raw("city", "e", "Changed tram notice", now + timedelta(minutes=30)),
        ]
        session.add_all(raws)
        session.flush()
        posts = [_post(raw.title) for raw in raws]
        session.add_all(posts)
        session.flush()
        runs = [
            _run(raws[0], posts[0]),
            _run(raws[1], posts[1], delivery="sent"),
            _run(raws[2], posts[2], delivery="uncertain"),
            _run(raws[3], posts[3]),
        ]
        runs[1].staging_message_id = 77
        posts[1].telegram_message_id = 77
        posts[1].published_at = now + timedelta(minutes=15)
        session.add_all(runs)
        session.add(
            CollectorFailure(
                source_id="city",
                strategy="rss",
                error_type="TimeoutError",
                message="old source error",
                occurred_at=now - timedelta(hours=1),
            )
        )
        session.add_all(
            [
                SourceRevision(
                    raw_item_id=raws[2].id,
                    snapshot_sha256="e" * 64,
                    evidence={"changed": True},
                    observed_at=runs[2].created_at + timedelta(minutes=1),
                ),
                SourceRevision(
                    raw_item_id=raws[3].id,
                    snapshot_sha256="f" * 64,
                    evidence={"changed": True},
                    observed_at=runs[3].created_at + timedelta(minutes=1),
                ),
            ]
        )
        session.commit()

        payload = build_newsroom_inbox(session, city="belgrade")

    states = {item["title"]: item for item in payload["items"]}
    assert states["Ready transport notice"]["state"] == "ready"
    assert states["Ready transport notice"]["retry"] == {
        "automatic_retry": False,
        "policy": "none",
    }
    assert states["Sent road notice"]["state"] == "sent"
    assert states["Sent road notice"]["retry"] == {
        "automatic_retry": False,
        "policy": "none",
    }
    assert states["Uncertain bus notice"]["state"] == "uncertain"
    assert states["Uncertain bus notice"]["retry"] == {
        "automatic_retry": False,
        "policy": "manual_reconciliation",
    }
    assert states["Changed tram notice"]["state"] == "needs_reverify"
    assert states["Changed tram notice"]["reason_code"] == "source_revision_after_review"
    assert states["Changed tram notice"]["source_revision_count"] == 1


def test_inbox_groups_one_event_across_sources_and_prefers_reviewed_anchor() -> None:
    engine = _engine()
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([_source("city"), _source("venue")])
        first = _raw("city", "f", "Closure on Knez Mihailova tonight", now)
        second = _raw(
            "venue",
            "g",
            "Closure on Knez Mihailova tonight",
            now + timedelta(minutes=5),
        )
        session.add_all([first, second])
        session.flush()
        post = _post(second.title)
        session.add(post)
        session.flush()
        run = _run(second, post)
        run.evidence = {
            "city": "belgrade",
            "event_key": "reviewed-event-key",
            "documents": [
                {"raw_item_id": first.id},
                {"raw_item_id": second.id},
            ],
        }
        session.add(run)
        session.commit()

        payload = build_newsroom_inbox(session, city="belgrade")

    assert payload["total_events"] == 1
    item = payload["items"][0]
    assert item["event_key"] == "reviewed-event-key"
    assert item["anchor_raw_item_id"] == second.id
    assert item["raw_item_ids"] == [first.id, second.id]
    assert item["evidence"] == {
        "raw_item_ids": [first.id, second.id],
        "document_count": 2,
    }
    assert {source["source_id"] for source in item["sources"]} == {"city", "venue"}
