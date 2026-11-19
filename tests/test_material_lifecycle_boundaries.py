"""Закрытый материал нельзя восстановить обходным редакционным маршрутом."""

import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from test_channel_delivery import FakePublisher
from test_channel_delivery import reviewed as _reviewed
from test_gpt_batch_fallback import _rank, _record_rank
from test_gpt_batch_fallback import batch as _batch
from test_gpt_fallback_service import NOW, package_for_request
from test_gpt_fallback_service import fallback_session as _fallback_session

from relocant_media.ai.package import EditorialPackage
from relocant_media.ops.newsroom_export import city_snapshot
from relocant_media.scheduler import cli
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import DeliveryBlockedError
from relocant_media.services.gpt_batch_fallback import build_batch_request, validate_batch_rank
from relocant_media.services.gpt_fallback import (
    FallbackValidationError,
    _failed_ids,
    build_request,
    pending_gpt_ids,
    validate_fallback_candidate,
    validate_fallback_package,
)
from relocant_media.services.gpt_refresh import _require_unresolved
from relocant_media.services.material_lifecycle import (
    before_attempt,
    can_retry_snapshot,
    lifecycle_state,
)
from relocant_media.services.owner_review import (
    _validated_pending_package,
    record_owner_callback,
    record_owner_reply,
)
from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission

reviewed = _reviewed
batch = _batch
fallback_session = _fallback_session


def close_material(session, raw_id, *, now, expired=False):
    raw = session.get(RawItem, raw_id)
    source = session.get(Source, raw.source_id)
    state = "waiting" if expired else "blocked"
    record = SchedulerRun(
        task_key=f"material-lifecycle:{source.city}:{raw_id}",
        task_type="material_lifecycle", city=source.city,
        scheduled_for=now, started_at=now, attempt=1, status=state,
        details={"schema_version": 1, "policy_version": "material-lifecycle-v1",
                 "raw_item_id": raw_id, "state": state,
                 "attempts": 0, "fallback_attempts": 0, "rejection_audits": 0,
                 "audit_attempts": 0, "delivery_attempts": 0, "next_action": "prepare",
                 "next_attempt_at": (now-timedelta(minutes=1)).isoformat(),
                 "first_seen_at": (now-timedelta(hours=7)).isoformat(),
                 "deadline_at": (now-timedelta(seconds=1) if expired
                                 else now+timedelta(hours=1)).isoformat()},
    )
    session.add(record)
    session.commit()
    return record


@pytest.mark.parametrize("expired", [False, True])
def test_package_reimport_and_owner_validation_cannot_revive_material(reviewed, expired):
    session, run = reviewed
    package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    close_material(session, run.raw_item_id, now=datetime.now(UTC), expired=expired)
    prior = dict(run.outcome)
    with pytest.raises(PackageConflictError):
        import_package(session, package)
    with pytest.raises(DeliveryBlockedError):
        _validated_pending_package(session, run.raw_item_id)
    assert run.outcome == prior
    assert run.delivery_status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_delivery_guard_prevents_network_but_keeps_sent_idempotency(reviewed, expired):
    session, run = reviewed
    close_material(session, run.raw_item_id, now=datetime.now(UTC), expired=expired)
    publisher = FakePublisher()
    with pytest.raises(DeliveryBlockedError):
        await deliver_reviewed(session, run.raw_item_id, publisher,
                               target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert publisher.calls == []
    run.delivery_status = "sent"
    post = session.get(Post, run.post_id)
    post.telegram_message_id = 42
    post.published_at = datetime.now(UTC)
    session.commit()
    result = await deliver_reviewed(session, run.raw_item_id, publisher,
                                    target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert result.message_id == 42
    assert publisher.calls == []
    package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    assert import_package(session, package).delivery_status == "sent"


@pytest.mark.parametrize("expired", [False, True])
def test_old_owner_buttons_and_replies_cannot_revive_material(reviewed, expired):
    session, run = reviewed
    review = Submission(kind="owner_editorial_review", status="pending",
                        telegram_user_id=9001, telegram_message_id=501,
                        payload={"raw_item_id": run.raw_item_id, "owner_chat_id": 9001})
    session.add(review)
    session.commit()
    close_material(session, run.raw_item_id, now=datetime.now(UTC), expired=expired)
    result = record_owner_callback(session, {
        "from": {"id": 9001, "is_bot": False}, "data": f"or:publish:{review.id}",
        "message": {"message_id": 501, "chat": {"id": 9001}},
    }, owner_user_id=9001, owner_chat_id=9001)
    assert result is not None and not result.accepted
    result = record_owner_reply(session, {
        "from": {"id": 9001, "is_bot": False}, "chat": {"id": 9001},
        "reply_to_message": {"message_id": 501}, "text": "Исправить заголовок",
    }, owner_user_id=9001, owner_chat_id=9001)
    assert result is not None and not result.accepted
    assert review.status == "pending"


@pytest.mark.parametrize("expired", [False, True])
def test_fallback_preview_validation_and_refresh_respect_ledger(fallback_session, expired):
    session, raw, _ = fallback_session
    request = build_request(session, now=NOW)
    assert request is not None
    close_material(session, raw.id, now=NOW, expired=expired)
    before = list(session.scalars(select(SchedulerRun.id)))
    assert build_request(session, now=NOW) is None
    assert raw.id not in pending_gpt_ids(session, now=NOW)
    with pytest.raises(FallbackValidationError):
        validate_fallback_candidate(session, request, raw.id, now=NOW)
    with pytest.raises(FallbackValidationError):
        _require_unresolved(session, raw.id)
    assert list(session.scalars(select(SchedulerRun.id))) == before
    assert not session.dirty and not session.new


def test_already_ranked_batch_replay_cannot_select_closed_material(batch):
    session, raw, _, run = batch
    request = build_batch_request(session, now=NOW, scheduler_run_id=run.id)
    rank = _rank(request, [raw.id])
    _record_rank(session, run, request, rank)
    close_material(session, raw.id, now=NOW)
    with pytest.raises(FallbackValidationError):
        validate_batch_rank(session, request, rank, now=NOW)


def test_confirmed_sent_fallback_package_replay_keeps_exact_receipt(fallback_session):
    session, raw, _ = fallback_session
    request = build_request(session, now=NOW)
    package = package_for_request(request)
    run = import_package(session, package, now=NOW)
    run.delivery_status = "sent"
    run.staging_message_id = 777
    session.commit()
    record = close_material(session, raw.id, now=NOW)
    record.details = {**record.details, "state": "published"}
    session.commit()
    checked = validate_fallback_package(session, request, package, now=NOW)
    assert import_package(session, checked, now=NOW).staging_message_id == 777
    assert build_request(session, now=NOW) is None


@pytest.mark.parametrize("refuted", [False, True])
def test_only_audited_refuted_empty_rejection_can_enter_fallback(fallback_session, refuted):
    session, raw, cycle = fallback_session
    cycle.details = {**cycle.details, "processing_errors": [], "lifecycle_fallback_ids": [raw.id]}
    lifecycle_state(session, raw.id, now=NOW)
    ledger = session.scalar(select(SchedulerRun).where(
        SchedulerRun.task_type == "material_lifecycle"))
    ledger.details = {**ledger.details, "state": "waiting_fallback", "next_action": "fallback",
                      "attempts": 2, "rejection_audits": int(refuted),
                      "rejection_justified": False if refuted else None,
                      "fallback_deadline_at": (NOW+timedelta(minutes=30)).isoformat()}
    session.add(EditorialRun(raw_item_id=raw.id, delivery_status="pending",
                             evidence={}, outcome={"status": "skipped",
                                                   "ranking": {"category": "transport"}}))
    session.commit()
    assert can_retry_snapshot(session, raw.id, now=NOW) is refuted
    request = build_request(session, now=NOW)
    assert (request is not None) is refuted
    assert (raw.id in pending_gpt_ids(session, now=NOW)) is refuted
    assert not session.new and not session.dirty
    if refuted:
        package = package_for_request(request)
        validate_fallback_package(session, request, package, now=NOW)
        assert before_attempt(session, raw.id, now=NOW, channel="fallback")
        session.commit()
        validate_fallback_package(session, request, package, now=NOW)


def test_ledger_is_excluded_before_public_and_cli_run_limits(reviewed, monkeypatch):
    session, run = reviewed
    now = datetime.now(UTC)
    visible = SchedulerRun(task_key="editorial-primary:belgrade", task_type="editorial_primary",
                           city="belgrade", scheduled_for=now, started_at=now,
                           attempt=1, status="success", details={})
    session.add(visible)
    session.commit()
    for offset in range(35):
        record = close_material(session, run.raw_item_id, now=now+timedelta(seconds=offset))
        record.task_key += f":test:{offset}"
        session.commit()
    projection = city_snapshot(Path.cwd(), "belgrade", session)
    assert [row["id"] for row in projection["runs"]] == [visible.id]
    engine = session.get_bind()
    from sqlalchemy.orm import Session

    monkeypatch.setattr(cli, "SessionFactory", lambda: Session(engine))
    status = cli._status(Namespace(city="belgrade", limit=1))
    assert [row["id"] for row in status["runs"]] == [visible.id]


@pytest.mark.parametrize("ids", [[True], ["1"], [1, 1], [1, 2, 3, 4, 5, 6], [-1]])
def test_fallback_dispatch_rejects_malformed_lifecycle_lists(fallback_session, ids):
    _, _, cycle = fallback_session
    cycle.details = {"processing_errors": [], "lifecycle_fallback_ids": ids}
    assert _failed_ids(cycle, NOW) == []


def test_lifecycle_dispatch_requires_a_completed_trusted_primary_cycle(fallback_session):
    _, raw, cycle = fallback_session
    cycle.details = {"lifecycle_fallback_ids": [raw.id]}
    assert _failed_ids(cycle, NOW) == [raw.id]
    cycle.finished_at = None
    assert _failed_ids(cycle, NOW) == []
