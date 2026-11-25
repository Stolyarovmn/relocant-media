from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.scheduler.models import SchedulerRun
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
        session.flush()
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
        session.flush()
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
        session.flush()
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


def _closed(raw_id, now, **details):
    return SchedulerRun(task_type="material_lifecycle", task_key=f"closed-{raw_id}",
        city="belgrade", scheduled_for=now, started_at=now, status="blocked", attempt=1,
        details={"raw_item_id": raw_id, "state": "blocked", "reason": "owner_closed_backlog",
                 "attempt_records": "PRIVATE", **details})


def test_closed_unprocessed_material_is_held_without_retry_or_private_fields():
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("city"))
        session.flush()
        raw = _raw("city", "a", "A unique notice", now)
        session.add(raw)
        session.flush()
        session.add_all([_closed(raw.id, now), CollectorFailure(source_id="city",
            strategy="test", error_type="TimeoutError", message="fake", occurred_at=now)])
        session.commit()
        result = build_newsroom_inbox(session)
        item = result["items"][0]
        assert item["state"] == "held" and item["reason_code"] == "owner_closed_backlog"
        assert item["retry"] == {"automatic_retry": False, "policy": "none"}
        assert "PRIVATE" not in str(result) and session.get(EditorialRun, raw.id) is None


def test_mixed_cluster_uses_new_active_member_after_old_anchor_is_closed():
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([_source("city"), _source("venue")])
        session.flush()
        old = _raw("city", "a", "Closure on Knez Mihailova tonight", now)
        new = _raw("venue", "b", old.title, now + timedelta(minutes=5))
        session.add_all([old, new])
        session.flush()
        session.add(_closed(old.id, now))
        session.commit()
        item = build_newsroom_inbox(session)["items"][0]
        assert item["state"] == "evidence_ready" and item["anchor_raw_item_id"] == new.id


@pytest.mark.parametrize("delivery", ["sending", "uncertain", "sent"])
def test_transport_on_another_closed_member_remains_visible(delivery):
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([_source("city"), _source("venue")])
        session.flush()
        old = _raw("city", "a", "Closure on Knez Mihailova tonight", now)
        sent = _raw("venue", "b", old.title, now + timedelta(minutes=5))
        session.add_all([old, sent])
        session.flush()
        post = _post(old.title)
        session.add(post)
        session.flush()
        if delivery == "sent":
            post.telegram_message_id = 12
        session.add_all([_closed(old.id, now), _closed(sent.id, now),
                         _run(sent, post, delivery=delivery)])
        session.commit()
        item = build_newsroom_inbox(session)["items"][0]
        assert item["state"] == delivery and item["anchor_raw_item_id"] == sent.id
        assert item["retry"]["automatic_retry"] is False


def test_closed_source_alias_uses_root_without_exporting_history():
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([_source("city"), _source("venue")])
        session.flush()
        root = _raw("city", "a", "Closure on Knez Mihailova tonight", now)
        alias = _raw("venue", "b", root.title, now + timedelta(minutes=5))
        session.add_all([root, alias])
        session.flush()
        session.add_all([_closed(root.id, now), _closed(alias.id, now, state="alias",
                         lifecycle_root_raw_id=root.id)])
        session.commit()
        result = build_newsroom_inbox(session)
        assert result["items"][0]["state"] == "held"
        assert "PRIVATE" not in str(result)


@pytest.mark.parametrize("message_id", [None, 0, -1])
def test_sent_without_valid_receipt_stays_visible_for_reconciliation(message_id):
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("city"))
        session.flush()
        raw = _raw("city", "a", "A unique notice", now)
        session.add(raw)
        session.flush()
        post = _post(raw.title)
        post.telegram_message_id = message_id
        session.add(post)
        session.flush()
        session.add_all([_closed(raw.id, now), _run(raw, post, delivery="sent")])
        session.commit()
        result = build_newsroom_inbox(session)
        item = result["items"][0]
        assert item["state"] == "uncertain"
        assert item["retry"]["policy"] == "manual_reconciliation"
        assert result["delivery_checks"] == {
            "uncertain_send": 0, "sending": 0, "sent_without_receipt": 1, "receipt_conflict": 0,
        }


@pytest.mark.parametrize(("staging_id", "has_publication_time", "state", "missing_count"), [
    (2, False, "held", 0),
    (0, False, "uncertain", 1),
    (2, True, "uncertain", 1),
])
def test_test_chat_receipt_does_not_count_as_uncertain_channel_send(
    staging_id, has_publication_time, state, missing_count
):
    engine, now = _engine(), datetime(2026, 10, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("city"))
        session.flush()
        raw = _raw("city", "a", "A unique notice", now)
        post = _post(raw.title)
        if has_publication_time:
            post.published_at = now + timedelta(minutes=7)
        session.add_all([raw, post])
        session.flush()
        run = _run(raw, post, delivery="sent")
        run.staging_message_id = staging_id
        session.add_all([run, CollectorFailure(
            source_id="city", strategy="rss", error_type="TimeoutError",
            message="Source failure", occurred_at=now + timedelta(hours=1),
        )])
        session.commit()

        result = build_newsroom_inbox(session)
        item = result["items"][0]
        assert item["state"] == state
        assert item["telegram_message_id"] is None
        assert item["retry"]["automatic_retry"] is False
        assert result["delivery_checks"]["sent_without_receipt"] == missing_count
        if state == "held":
            assert item["reason_code"] == "staging_delivery_only"
            assert item["retry"]["policy"] == "none"
            assert run.delivery_status == "sent"
            assert run.staging_message_id == staging_id


@pytest.mark.parametrize("message_id", [11, None])
@pytest.mark.parametrize(("delivery", "check"), [
    ("uncertain", "uncertain_send"), ("sending", "sending"),
])
def test_incomplete_member_has_priority_over_sent_member_of_cluster(message_id, delivery, check):
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([_source("city"), _source("venue")])
        session.flush()
        sent = _raw("city", "a", "Closure on Knez Mihailova tonight", now)
        uncertain = _raw("venue", "b", sent.title, now + timedelta(minutes=5))
        session.add_all([sent, uncertain])
        session.flush()
        first, second = _post(sent.title), _post(sent.title)
        first.telegram_message_id = message_id
        session.add_all([first, second])
        session.flush()
        session.add_all([_closed(sent.id, now), _closed(uncertain.id, now),
            _run(sent, first, delivery="sent"), _run(uncertain, second, delivery=delivery)])
        session.commit()
        result = build_newsroom_inbox(session)
        item = result["items"][0]
        assert item["state"] == delivery and item["anchor_raw_item_id"] == uncertain.id
        assert result["delivery_checks"][check] == 1
        assert result["delivery_checks"]["sent_without_receipt"] == 0


def test_delivery_checks_include_the_queue_outside_the_display_limit():
    engine, now = _engine(), datetime(2026, 11, 19, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("city"))
        session.flush()
        for index, (title, status, message) in enumerate([
            ("Изменение маршрута автобуса 20", "uncertain", None),
            ("Открытие фестиваля керамики", "sent", None),
            ("Подорожание билетов аэропорта", "pending", 42),
            ("Временное отключение воды", "sending", None),
        ]):
            raw = _raw("city", str(index), title, now + timedelta(hours=index))
            post = _post(title)
            post.telegram_message_id = message
            session.add_all([raw, post])
            session.flush()
            session.add(_run(raw, post, delivery=status))
        result = build_newsroom_inbox(session, limit=1)
        assert len(result["items"]) == 1 and result["total_events"] == 4
        assert result["delivery_checks"] == {
            "uncertain_send": 1, "sending": 1, "sent_without_receipt": 1, "receipt_conflict": 1,
        }
