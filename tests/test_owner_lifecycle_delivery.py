import copy
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_owner_review import (
    NOW,
    OWNER,
    OWNER_CHAT,
    Publisher,
    ReviewBot,
    _live,
    _owner_review_row,
    _store_reviewed_politics,
)
from test_owner_review import engine as engine

from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.material_lifecycle import lifecycle_state
from relocant_media.services.owner_review import process_owner_actions
from relocant_media.storage.models import EditorialRun, Post, Submission


@pytest.fixture
def clock(monkeypatch):
    current = [NOW]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].replace(tzinfo=None) if tz is None else current[0].astimezone(tz)

    monkeypatch.setattr("relocant_media.services.owner_review.datetime", FixedDateTime)
    return current


def approve_synthetic_review(engine):
    raw_id = _store_reviewed_politics(engine, precision="day")
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        review.status = "approved"
        review.payload = {**review.payload, "owner_decision": "publish"}
        review_id = review.id
        session.commit()
    return raw_id, review_id


async def run(engine, fetcher, publisher):
    return await process_owner_actions(
        engine, ReviewBot(), owner_user_id=OWNER, owner_chat_id=OWNER_CHAT,
        provider=None, live_document_fetcher=fetcher, publisher=publisher, publish_enabled=True,
    )


@pytest.mark.asyncio
async def test_owner_live_fetch_has_durable_claim_bounded_retry_and_real_approval(engine, clock):
    raw_id, review_id = approve_synthetic_review(engine)
    calls = []

    def failing_live(package):
        with Session(engine) as session:
            row = session.scalar(select(SchedulerRun).where(
                SchedulerRun.task_type == "material_lifecycle"))
            assert row.details["state"] == "processing"
            assert row.details["attempt_kind"] == "owner_delivery"
            calls.append(row.details["delivery_attempts"])
        raise httpx.ConnectError("Недоступен синтетический источник")

    publisher = Publisher()
    with Session(engine) as session:
        original = copy.deepcopy(session.get(EditorialRun, raw_id).outcome)
    await run(engine, failing_live, publisher)
    with Session(engine) as session:
        state = lifecycle_state(session, raw_id, now=NOW)
        assert state["next_action"] == "owner_delivery"
        assert state["delivery_attempts"] == 1
        assert datetime.fromisoformat(state["next_attempt_at"]) == NOW+timedelta(minutes=5)
        assert session.get(EditorialRun, raw_id).outcome == original
        assert session.get(Submission, review_id).status == "approved"
        session.commit()
    for minute in (0, 1, 4):
        clock[0] = NOW+timedelta(minutes=minute)
        await run(engine, failing_live, publisher)
    assert calls == [1]
    clock[0] = NOW+timedelta(minutes=5)
    await run(engine, failing_live, publisher)
    for minute in (6, 20):
        clock[0] = NOW+timedelta(minutes=minute)
        await run(engine, failing_live, publisher)
    assert calls == [1, 2] and publisher.calls == []
    with Session(engine) as session:
        state = lifecycle_state(session, raw_id, now=clock[0])
        assert state["state"] == "blocked"
        assert state["reason"] == "delivery_attempt_budget_exhausted"
        assert session.get(EditorialRun, raw_id).outcome == original
        assert session.get(Submission, review_id).status == "approved"


@pytest.mark.asyncio
async def test_owner_retry_can_publish_once_then_receipt_is_terminal(engine, clock):
    raw_id, review_id = approve_synthetic_review(engine)
    calls = []

    def first_failure(package):
        calls.append(package.raw_item_id)
        if len(calls) == 1:
            raise ValueError("Источник временно недоступен")
        return _live(package)

    publisher = Publisher()
    await run(engine, first_failure, publisher)
    clock[0] = NOW+timedelta(minutes=5)
    result = await run(engine, first_failure, publisher)
    assert result["published"] == 1 and len(publisher.calls) == 1
    await run(engine, first_failure, publisher)
    assert len(calls) == 2 and len(publisher.calls) == 1
    with Session(engine) as session:
        review = session.get(Submission, review_id)
        editorial = session.get(EditorialRun, raw_id)
        post = session.get(Post, editorial.post_id)
        assert review.status == "published" and editorial.delivery_status == "sent"
        assert post.telegram_message_id == 777
        assert lifecycle_state(session, raw_id, now=clock[0])["state"] == "published"


@pytest.mark.asyncio
async def test_owner_uncertain_send_is_blocked_without_another_send(engine, clock):
    raw_id, review_id = approve_synthetic_review(engine)

    class UncertainPublisher:
        calls = 0

        async def publish(self, text):
            self.calls += 1
            raise httpx.ReadTimeout("Нет квитанции синтетической отправки")

    publisher = UncertainPublisher()
    assert (await run(engine, _live, publisher))["blocked"] == 1
    clock[0] = NOW+timedelta(minutes=5)
    await run(engine, _live, publisher)
    assert publisher.calls == 1
    with Session(engine) as session:
        assert session.get(EditorialRun, raw_id).delivery_status == "uncertain"
        assert session.get(Submission, review_id).status == "approved"
        state = lifecycle_state(session, raw_id, now=clock[0])
        assert state["state"] == "blocked"
        assert state["reason"] == "delivery_reconciliation_required"
