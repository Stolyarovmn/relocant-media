import copy
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from facts_check_helpers import reviewed_package_for
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from test_bgsaobracaj_notices import ARTICLE, PUBLISHED, SCHEDULE
from test_editorial_package import bind_context

from relocant_media.ai.package import canonical_json
from relocant_media.collectors.beograd_article import parse_beograd_article
from relocant_media.collectors.bgsaobracaj_notices import SOURCE_ID
from relocant_media.domain.policy import SOURCE_REPLACED_REASON
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.material_lifecycle import (
    MATERIAL_TTL,
    LifecycleBlockedError,
    adopt_fallback_request,
    after_error,
    after_result,
    archive_for_retry,
    authorize_refreshed_evidence,
    authorize_retry,
    before_attempt,
    before_delivery_attempt,
    can_retry_snapshot,
    delivery_failure,
    ensure_not_terminal,
    inherit_lifecycle,
    is_terminal_blocked,
    lifecycle_state,
    rejection_reviewed,
    sweep_materials,
)
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source, Submission

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Source(id="city", city="belgrade", name="Город", url="https://example.test",
                           source_type="official", trust=1, ownership="official",
                           auto_publish=True, high_risk=False, enabled=True))
        session.flush()
        session.add(raw(1))
        session.commit()
        yield session
    engine.dispose()


def raw(raw_id):
    return RawItem(id=raw_id, source_id="city", external_id=str(raw_id),
                   url=f"https://example.test/{raw_id}", title="Изменение маршрута автобуса",
                   body="Город сообщил об изменении автобусного маршрута.",
                   published_at=NOW, published_at_precision="instant", ingested_at=NOW)


def editorial(session, *, status="skipped", category="transport", post=False, **fields):
    record = EditorialRun(raw_item_id=1, delivery_status="pending", created_at=NOW,
                          outcome={"status": status, "reason": "test_decision",
                                   "ranking": {"category": category}, **fields},
                          evidence={"documents": [{"source_id": "city", "url": "synthetic"}]})
    if post:
        item = Post(city="belgrade", category=category, status=status,
                    title="Городская новость", body="Проверенный текст", confidence=1)
        session.add(item)
        session.flush()
        record.post_id = item.id
    session.add(record)
    session.flush()
    return record


def test_new_material_has_finite_window_not_based_on_old_ingestion(session):
    session.get(RawItem, 1).ingested_at = NOW-timedelta(days=30)
    result = sweep_materials(session, city="belgrade", now=NOW)
    state = lifecycle_state(session, 1, now=NOW)
    assert result.due_ids == (1,)
    assert datetime.fromisoformat(state["deadline_at"]) == NOW+MATERIAL_TTL
    session.commit()
    session.expire_all()
    assert lifecycle_state(session, 1, now=NOW+MATERIAL_TTL)["state"] == "blocked"
    assert not before_attempt(session, 1, now=NOW+MATERIAL_TTL)


def test_bounded_primary_then_fallback_survives_session_reload(session):
    assert before_attempt(session, 1, now=NOW)
    after_error(session, 1, "ProviderTimeout", now=NOW)
    session.commit()
    session.expire_all()
    assert not before_attempt(session, 1, now=NOW+timedelta(minutes=4))
    retry = NOW+timedelta(minutes=5)
    assert before_attempt(session, 1, now=retry)
    state = after_error(session, 1, "ProviderTimeout", now=retry).details
    assert state["attempts"] == 2 and state["next_action"] == "fallback"
    assert before_attempt(session, 1, now=retry, channel="fallback")
    state = after_error(session, 1, "FallbackTimeout", now=retry).details
    assert state["state"] == "blocked" and state["fallback_attempts"] == 1
    assert not before_attempt(session, 1, now=retry+timedelta(minutes=5))


def test_early_existing_fallback_cannot_gain_new_budget(session):
    assert not before_attempt(session, 1, now=NOW, channel="fallback")
    assert before_attempt(session, 1, now=NOW)
    after_error(session, 1, "ProviderTimeout", now=NOW)
    deadline = lifecycle_state(session, 1, now=NOW)["deadline_at"]
    assert before_attempt(session, 1, now=NOW, channel="fallback")
    state = after_error(session, 1, "FallbackTimeout", now=NOW).details
    assert state["deadline_at"] == deadline and state["state"] == "blocked"
    assert not adopt_fallback_request(session, 1, now=NOW)


def test_trusted_legacy_fallback_adoption_keeps_original_deadline(session):
    deadline = lifecycle_state(session, 1, now=NOW)["deadline_at"]
    later = NOW+timedelta(minutes=20)
    assert adopt_fallback_request(session, 1, now=later)
    assert before_attempt(session, 1, now=later, channel="fallback")
    state = lifecycle_state(session, 1, now=later)
    assert state["attempts"] == 1 and state["fallback_attempts"] == 1
    assert state["deadline_at"] == deadline


def test_repeated_fallback_request_cannot_extend_its_shorter_deadline(session):
    assert adopt_fallback_request(session, 1, now=NOW)
    deadline = lifecycle_state(session, 1, now=NOW)["fallback_deadline_at"]
    assert adopt_fallback_request(session, 1, now=NOW+timedelta(minutes=20))
    assert lifecycle_state(session, 1, now=NOW)["fallback_deadline_at"] == deadline
    assert not before_attempt(session, 1, now=NOW+timedelta(minutes=30), channel="fallback")


def test_crashed_attempt_has_lease_and_does_not_reset_count(session):
    assert before_attempt(session, 1, now=NOW)
    state = lifecycle_state(session, 1, now=NOW+timedelta(minutes=15))
    assert state["attempts"] == 1 and state["next_action"] == "retry_prepare"
    assert not before_attempt(session, 1, now=NOW+timedelta(minutes=19))
    assert before_attempt(session, 1, now=NOW+timedelta(minutes=20))


@pytest.mark.parametrize("status", ["skipped", "blocked"])
def test_rejected_material_is_audited_and_safe_retry_archives_exact_decision(session, status):
    assert before_attempt(session, 1, now=NOW)
    run = editorial(session, status=status)
    old_outcome, old_evidence = copy.deepcopy(run.outcome), copy.deepcopy(run.evidence)
    assert after_result(session, 1, now=NOW)["next_action"] == "audit_rejection"
    assert not authorize_retry(session, 1, now=NOW)
    assert before_attempt(session, 1, now=NOW, channel="rejection_audit")
    state = rejection_reviewed(session, 1, justified=False, now=NOW)
    assert state["next_action"] == "retry_prepare"
    assert before_attempt(session, 1, now=NOW)
    assert archive_for_retry(session, 1, now=NOW)
    assert not archive_for_retry(session, 1, now=NOW)
    state = lifecycle_state(session, 1, now=NOW)
    assert state["attempt_records"][0]["outcome"] == old_outcome
    assert state["attempt_records"][0]["evidence"] == old_evidence
    assert session.get(EditorialRun, 1) is None
    assert state["attempts"] == 2
    editorial(session)
    assert after_result(session, 1, now=NOW)["next_action"] == "audit_rejection"
    assert before_attempt(session, 1, now=NOW, channel="rejection_audit")
    state = rejection_reviewed(session, 1, justified=False, now=NOW)
    assert state["next_action"] == "fallback"
    assert state["attempts"] == state["audit_attempts"] == state["rejection_audits"] == 2
    assert not before_attempt(session, 1, now=NOW, channel="primary")
    assert can_retry_snapshot(session, 1, now=NOW)
    assert archive_for_retry(session, 1, now=NOW)
    assert before_attempt(session, 1, now=NOW, channel="fallback")
    editorial(session, reason="gpt_ranking_rejected")
    state = after_result(session, 1, now=NOW)
    assert state["state"] == "blocked" and state["reason"] == "fallback_editorial_rejection"
    assert state["fallback_attempts"] == 1
    assert not before_attempt(session, 1, now=NOW, channel="rejection_audit")


def test_rejection_audit_errors_are_bounded_without_fake_confirmation(session):
    editorial(session)
    assert before_attempt(session, 1, now=NOW, channel="rejection_audit")
    state = after_error(session, 1, "AuditTimeout", now=NOW,
                        channel="rejection_audit").details
    assert state["next_action"] == "audit_rejection" and state["rejection_audits"] == 0
    assert not before_attempt(session, 1, now=NOW, channel="rejection_audit")
    retry = NOW+timedelta(minutes=5)
    assert before_attempt(session, 1, now=retry, channel="rejection_audit")
    state = after_error(session, 1, "AuditTimeout", now=retry,
                        channel="rejection_audit").details
    assert state["state"] == "waiting_fallback"
    assert state["rejection_audits"] == 0 and state["audit_attempts"] == 2
    assert state["attempts"] == 0 and state["reason"] == "rejection_audit_budget_exhausted"
    assert state["rejection_justified"] is None
    assert can_retry_snapshot(session, 1, now=retry)
    assert [item["error_type"] for item in state["rejection_review_history"]] == [
        "AuditTimeout", "AuditTimeout"]
    deadline = state["fallback_deadline_at"]
    assert adopt_fallback_request(session, 1, now=retry+timedelta(minutes=1))
    assert lifecycle_state(session, 1, now=retry)["fallback_deadline_at"] == deadline
    assert not can_retry_snapshot(session, 1, now=retry+timedelta(minutes=30))


def test_refuted_rejection_can_use_fallback_but_never_reopen_terminal(session):
    assert before_attempt(session, 1, now=NOW)
    after_error(session, 1, "Timeout", now=NOW)
    now = NOW+timedelta(minutes=5)
    assert before_attempt(session, 1, now=now)
    editorial(session)
    after_result(session, 1, now=now)
    assert before_attempt(session, 1, now=now, channel="rejection_audit")
    rejection_reviewed(session, 1, justified=False, now=now)
    session.commit()
    record = session.scalar(select(SchedulerRun))
    assert record.details["reason"] == "rejection_not_justified"
    snapshot = copy.deepcopy(record.details)
    assert can_retry_snapshot(session, 1, now=now)
    assert record.details == snapshot and not session.dirty
    assert before_attempt(session, 1, now=now, channel="fallback")
    assert archive_for_retry(session, 1, now=now)
    after_error(session, 1, "Timeout", now=now, channel="fallback")
    assert not can_retry_snapshot(session, 1, now=now)


def test_confirmed_rejection_is_terminal_and_preserves_evidence(session):
    run = editorial(session)
    evidence = copy.deepcopy(run.evidence)
    assert before_attempt(session, 1, now=NOW, channel="rejection_audit")
    state = rejection_reviewed(session, 1, justified=True, now=NOW)
    assert state["reason"] == "rejection_confirmed"
    assert run.evidence == evidence
    assert run.outcome["_lifecycle_block"]["previous_status"] == "skipped"
    assert not archive_for_retry(session, 1, now=NOW)


def test_second_rejection_is_confirmed_independently_without_fallback(session):
    assert before_attempt(session, 1, now=NOW)
    editorial(session)
    after_result(session, 1, now=NOW)
    assert before_attempt(session, 1, now=NOW, channel="rejection_audit")
    rejection_reviewed(session, 1, justified=False, now=NOW)
    assert before_attempt(session, 1, now=NOW)
    assert archive_for_retry(session, 1, now=NOW)
    editorial(session, reason="different_new_rejection")
    assert after_result(session, 1, now=NOW)["next_action"] == "audit_rejection"
    assert before_attempt(session, 1, now=NOW, channel="rejection_audit")
    state = rejection_reviewed(session, 1, justified=True, now=NOW)
    assert state["reason"] == "rejection_confirmed" and state["state"] == "blocked"
    assert state["audit_attempts"] == 2 and state["fallback_attempts"] == 0
    assert not can_retry_snapshot(session, 1, now=NOW)


@pytest.mark.parametrize("reason", ["repeated_editorial_rejection", "owner_closed_backlog"])
def test_historical_terminal_rejections_are_never_reopened(session, reason):
    editorial(session)
    lifecycle_state(session, 1, now=NOW)
    record = session.scalar(select(SchedulerRun))
    record.details = {**record.details, "state": "blocked", "next_action": None,
                      "next_attempt_at": None, "reason": reason, "rejection_audits": 1}
    snapshot = copy.deepcopy(record.details)
    assert lifecycle_state(session, 1, now=NOW) == snapshot
    assert not before_attempt(session, 1, now=NOW, channel="rejection_audit")
    assert not adopt_fallback_request(session, 1, now=NOW)
    assert not can_retry_snapshot(session, 1, now=NOW)


@pytest.mark.parametrize("guard", ["source", "category", "disabled"])
def test_risk_never_enters_automatic_processing(session, guard):
    if guard == "source":
        session.get(Source, "city").high_risk = True
    elif guard == "disabled":
        session.get(Source, "city").auto_publish = False
    else:
        editorial(session, status="human_review", category="law")
    assert not before_attempt(session, 1, now=NOW)
    assert is_terminal_blocked(session, 1)
    with pytest.raises(LifecycleBlockedError):
        ensure_not_terminal(session, 1, now=NOW)


def test_owner_approved_risky_packet_keeps_manual_path_and_evidence(session):
    run = editorial(session, status="ready", category="law", post=True,
                    _package_sha256="a"*64)
    original = copy.deepcopy(run.outcome)
    session.add(Submission(kind="owner_editorial_review", status="approved",
                           payload={"raw_item_id": 1, "package_sha256": "a"*64}))
    session.flush()
    assert lifecycle_state(session, 1, now=NOW)["next_action"] == "owner_delivery"
    assert not before_delivery_attempt(session, 1, now=NOW)
    assert lifecycle_state(session, 1, now=NOW+MATERIAL_TTL)["state"] == "blocked"
    assert run.outcome == original
    assert session.get(Post, run.post_id).status == "ready"


def verified_utility_hold(session):
    run = editorial(session, status="human_review", category="utilities", post=True,
                    reason="package_requires_review")
    post = session.get(Post, run.post_id)
    payload = reviewed_package_for(session.get(RawItem, 1), session.get(Source, "city"),
                                   title=post.title, body=post.body, now=NOW)
    run.outcome = {**run.outcome, "draft": payload["outcome"]["draft"], "_package": payload,
                   "_package_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}
    return run


def test_verified_utility_waits_for_exact_owner_approval(session):
    run = verified_utility_hold(session)
    state = lifecycle_state(session, 1, now=NOW)
    assert state["next_action"] == "owner_approval"
    assert run.outcome["status"] == "human_review"
    assert not before_attempt(session, 1, now=NOW)
    assert not before_delivery_attempt(session, 1, now=NOW)

    session.add(Submission(kind="owner_editorial_review", status="approved",
                           payload={"raw_item_id": 1,
                                    "package_sha256": run.outcome["_package_sha256"]}))
    session.flush()
    assert lifecycle_state(session, 1, now=NOW)["next_action"] == "owner_delivery"


@pytest.mark.parametrize("obstacle", [None, "expired", "owner_decided", "source_changed"])
def test_only_fresh_unqueued_verified_utility_recovers_from_false_block(session, obstacle):
    run = verified_utility_hold(session)
    state = lifecycle_state(session, 1, now=NOW)
    record = session.scalar(select(SchedulerRun))
    record.status = "blocked"
    record.finished_at = NOW
    record.details = {**state, "state": "blocked", "next_action": None, "next_attempt_at": None,
                      "reason": "separate_publication_approval_required"}
    run.outcome = {**run.outcome, "status": "blocked",
                   "reason": "lifecycle_separate_publication_approval_required",
                   "_lifecycle_block": {"policy_version": "material-lifecycle-v1",
                                        "previous_status": "human_review",
                                        "previous_reason": "package_requires_review"}}
    post = session.get(Post, run.post_id)
    post.status = "blocked"
    checked = NOW + (MATERIAL_TTL if obstacle == "expired" else timedelta(minutes=5))
    if obstacle == "owner_decided":
        session.add(Submission(kind="owner_editorial_review", status="rejected",
                               payload={"raw_item_id": 1}))
    if obstacle == "source_changed":
        session.get(RawItem, 1).body += " Дополнение к объявлению."
    session.commit()

    sweep = sweep_materials(session, city="belgrade", now=checked)
    assert sweep.due_ids == sweep.fallback_ids == ()
    assert 1 in sweep.excluded_ids
    assert record.details["deadline_at"] == state["deadline_at"]
    if obstacle:
        assert sweep.blocked_ids == (1,)
        assert post.status == "blocked"
    else:
        assert sweep.blocked_ids == ()
        assert record.details["next_action"] == "owner_approval"
        assert post.status == run.outcome["status"] == "human_review"
        assert "_recovered_owner_hold" in run.outcome
        assert record.finished_at is None


@pytest.fixture
def ready_official_water(session):
    source = Source(id="city_beograd_news", city="belgrade", name="Город",
                    url="https://www.beograd.rs/", source_type="official", trust=1,
                    ownership="official", auto_publish=True, high_risk=False, enabled=True)
    session.add(source)
    session.flush()
    notice = session.get(RawItem, 1)
    notice.source_id = source.id
    notice.url = source.url + "lat/servisne-informacije/vodovod-i-kanalizacija/a1/x.html"
    notice.title = "Deo grada bez vode 20. novembra"
    notice.body = "Na uglu ulica, 20. novembra, potrošači će biti bez vode."
    run = editorial(session, status="ready", category="utilities", post=True,
                    reason="package_independently_reviewed")
    post = session.get(Post, run.post_id)
    post.title = "Отключение воды 20 ноября"
    post.body = "На перекрёстке отключат воду 20 ноября."
    payload = reviewed_package_for(notice, source, title=post.title, body=post.body, now=NOW)
    payload["outcome"]["ranking"].update(category="utilities", confidence=0.99)
    payload["outcome"]["confidence"] = 0.99
    payload["outcome"]["verification"]["claims"][0]["confidence"] = 0.99
    bind_context(payload)
    run.outcome = {**run.outcome, "draft": payload["outcome"]["draft"], "_package": payload,
                   "_package_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}
    return run, post, notice, source


def test_reviewed_water_notice_can_reach_channel_delivery_without_owner(
    session, ready_official_water,
):
    run, post, _, _ = ready_official_water
    assert lifecycle_state(session, 1, now=NOW)["next_action"] == "deliver"
    assert post.status == run.outcome["status"] == "ready"
    assert before_delivery_attempt(session, 1, now=NOW)


@pytest.mark.parametrize("obstacle", [
    None, "expired", "owner_decided", "source_changed", "source_disabled", "tampered",
    "attempted",
])
def test_only_fresh_verified_unsent_water_recovers_from_old_ready_block(
    session, ready_official_water, obstacle,
):
    run, post, notice, source = ready_official_water
    state = lifecycle_state(session, 1, now=NOW)
    record = session.scalar(select(SchedulerRun))
    record.status = "blocked"
    record.finished_at = NOW
    record.details = {**state, "state": "blocked", "next_action": None, "next_attempt_at": None,
                      "reason": "separate_publication_approval_required"}
    run.outcome = {**run.outcome, "status": "blocked",
                   "reason": "lifecycle_separate_publication_approval_required",
                   "_lifecycle_block": {"policy_version": "material-lifecycle-v1",
                                        "previous_status": "ready",
                                        "previous_reason": "package_independently_reviewed"}}
    post.status = "blocked"
    if obstacle == "owner_decided":
        session.add(Submission(kind="owner_editorial_review", status="rejected",
                               payload={"raw_item_id": 1}))
    elif obstacle == "source_changed":
        notice.body += " Дополнение к объявлению."
    elif obstacle == "source_disabled":
        source.auto_publish = False
    elif obstacle == "tampered":
        run.outcome = {**run.outcome, "_package_sha256": "0" * 64}
    elif obstacle == "attempted":
        record.details = {**record.details, "delivery_attempts": 1}
    checked = NOW + (MATERIAL_TTL if obstacle == "expired" else timedelta(minutes=5))
    session.commit()

    sweep = sweep_materials(session, city="belgrade", now=checked)
    assert record.details["deadline_at"] == state["deadline_at"]
    if obstacle:
        assert sweep.blocked_ids == (1,)
        assert post.status == "blocked"
        assert not before_delivery_attempt(session, 1, now=checked)
    else:
        assert sweep.blocked_ids == ()
        assert record.details["next_action"] == "deliver"
        assert post.status == run.outcome["status"] == "ready"
        assert "_recovered_ready_hold" in run.outcome
        assert record.finished_at is None
        assert before_delivery_attempt(session, 1, now=checked)


@pytest.mark.parametrize("obstacle", [
    None, "staging", "uncertain", "owner", "changed", "tampered", "other_flag", "ended", "timeout",
])
def test_premature_night_road_rejection_gets_one_fresh_attempt(session, obstacle):
    first_attempt = datetime(2026, 11, 23, 4, 19, tzinfo=UTC)
    checked = first_attempt + timedelta(hours=1)
    source = Source(id=SOURCE_ID, city="belgrade", name="Секретариат", url=ARTICLE,
                    source_type="official_transport", trust=1, ownership="official",
                    auto_publish=True, high_risk=False, enabled=True)
    session.add(source)
    session.flush()
    notice = session.get(RawItem, 1)
    notice.source_id, notice.url = source.id, ARTICLE
    notice.published_at = PUBLISHED.astimezone(UTC)
    notice.title = "Ограничение в Теразийском тоннеле"
    notice.body = SCHEDULE if obstacle != "ended" else SCHEDULE.replace(" и 23./24.11.2026", "")
    notice.ingested_at = first_attempt
    assert before_attempt(session, 1, now=first_attempt)
    run = editorial(session, status="human_review", category="transport", post=True,
                    reason="draft_coverage_ungrounded")
    post_id = run.post_id
    post = session.get(Post, post_id)
    payload = reviewed_package_for(notice, source, title=post.title, body=post.body,
                                   now=first_attempt)
    facts = payload["reviews"][0]
    facts.update(verdict="hold", score=0, flags=["expired_event", "draft_coverage_ungrounded"])
    facts["facts_check"]["segments"][0]["citations"] = []
    if obstacle == "other_flag":
        facts["flags"].append("claim_mismatch")
    payload["reviews"][1].update(verdict="reject", flags=["истёкший срок события"])
    run.evidence = {"documents": payload["event"]["documents"]}
    run.outcome = {**run.outcome, "draft": payload["outcome"]["draft"], "_package": payload,
                   "_package_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}
    state = after_result(session, 1, now=first_attempt)
    assert state["reason"] == "draft_coverage_ungrounded"
    if obstacle == "staging":
        run.staging_message_id = 120
    if obstacle == "uncertain":
        run.delivery_status = "uncertain"
    if obstacle == "owner":
        session.add(Submission(kind="owner_editorial_review", status="rejected",
                               payload={"raw_item_id": 1}))
    if obstacle == "changed":
        notice.body += " Есть новая версия."
    if obstacle == "tampered":
        run.outcome = {**run.outcome, "_package_sha256": "0" * 64}
    if obstacle == "timeout":
        checked = first_attempt + MATERIAL_TTL
    old = copy.deepcopy(run.outcome)
    session.commit()

    sweep = sweep_materials(session, city="belgrade", now=checked)
    state = lifecycle_state(session, 1, now=checked)
    assert state["deadline_at"] == (first_attempt + MATERIAL_TTL).isoformat()
    assert session.get(Post, post_id).status == "blocked"
    if obstacle:
        assert sweep.due_ids == ()
        assert state["state"] == "blocked"
        assert session.get(EditorialRun, 1).outcome == old
        return
    assert sweep.due_ids == (1,)
    assert state["attempt_records"][0]["outcome"] == old
    assert state["attempt_records"][0]["post_id"] == post_id
    assert session.get(EditorialRun, 1) is None
    assert before_attempt(session, 1, now=checked)
    assert not before_attempt(session, 1, now=checked)
    assert not before_delivery_attempt(session, 1, now=checked)
    state = after_error(session, 1, "GeminiWorkerError", now=checked).details
    assert state["next_action"] == "fallback"
    assert state["attempts"] == 2 and state["fallback_attempts"] == 0


def test_pending_owner_request_closes_as_cancelled_without_fake_owner_rejection(session):
    run = editorial(session, status="ready", post=True)
    payload = {"raw_item_id": 1, "reason": "Согласовать формулировку", "decision": None}
    review = Submission(kind="owner_editorial_review", status="postponed", payload=payload)
    session.add(review)
    session.flush()
    lifecycle_state(session, 1, now=NOW)
    lifecycle_state(session, 1, now=NOW+MATERIAL_TTL)
    assert review.status == "cancelled"
    assert {key: review.payload[key] for key in payload} == payload
    assert review.payload["_lifecycle_closure"]["previous_status"] == "postponed"
    assert run.outcome["status"] == "blocked"


@pytest.mark.parametrize("delivery", ["sending", "uncertain", "receipt"])
def test_uncertain_delivery_and_receipts_are_never_retried_or_overwritten(session, delivery):
    run = editorial(session, status="ready", post=True)
    post = session.get(Post, run.post_id)
    if delivery == "receipt":
        post.telegram_message_id = 123
    else:
        run.delivery_status = delivery
    original = copy.deepcopy(run.outcome)
    state = lifecycle_state(session, 1, now=NOW)
    assert state["state"] == "blocked" and state["reason"] == "delivery_reconciliation_required"
    assert not archive_for_retry(session, 1, now=NOW)
    assert not before_delivery_attempt(session, 1, now=NOW)
    assert run.outcome == original and post.status == "ready"


def test_confirmed_publication_skipped_without_extra_ledger_during_sweep(session):
    run = editorial(session, status="ready", post=True)
    run.delivery_status = "sent"
    session.get(Post, run.post_id).telegram_message_id = 123
    result = sweep_materials(session, city="belgrade", now=NOW)
    assert result.excluded_ids == {1}
    assert session.scalar(select(func.count()).select_from(SchedulerRun)) == 0
    assert lifecycle_state(session, 1, now=NOW)["state"] == "published"


def test_delivery_retries_keep_ready_package_and_observe_backoff(session):
    run = editorial(session, status="ready", post=True)
    payload = reviewed_package_for(session.get(RawItem, 1), session.get(Source, "city"), now=NOW)
    run.outcome = {**run.outcome, "_package": payload,
                   "_package_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}
    original = copy.deepcopy(run.outcome)
    assert before_delivery_attempt(session, 1, now=NOW)
    state = delivery_failure(session, 1, "LiveFetchError", now=NOW)
    assert state["next_action"] == "deliver" and state["delivery_attempts"] == 1
    assert run.outcome == original
    assert not before_delivery_attempt(session, 1, now=NOW+timedelta(minutes=4))
    retry = NOW+timedelta(minutes=5)
    assert before_delivery_attempt(session, 1, now=retry)
    assert delivery_failure(session, 1, "LiveFetchError", now=retry)["state"] == "blocked"
    assert session.get(Post, run.post_id).status == "blocked"


def test_post_cannot_be_deleted_by_primary_retry(session):
    run = editorial(session, status="human_review", post=True)
    payload = reviewed_package_for(session.get(RawItem, 1), session.get(Source, "city"), now=NOW)
    run.outcome = {**run.outcome, "_package": payload,
                   "_package_sha256": hashlib.sha256(canonical_json(payload).encode()).hexdigest()}
    now = NOW+timedelta(minutes=5)
    lifecycle_state(session, 1, now=NOW)
    assert before_attempt(session, 1, now=now)
    after_error(session, 1, "RepairTimeout", now=now)
    assert not archive_for_retry(session, 1, now=now+timedelta(minutes=5))
    assert session.get(EditorialRun, 1) is run and session.get(Post, run.post_id)


def test_replacement_shares_deadline_budget_and_preserves_source_marker(session):
    assert before_attempt(session, 1, now=NOW)
    after_error(session, 1, "SourceUnavailableError", now=NOW)
    initial = lifecycle_state(session, 1, now=NOW)
    session.add(raw(2))
    original = editorial(session, reason=SOURCE_REPLACED_REASON, replacement_raw_item_id=2)
    session.flush()
    inherit_lifecycle(session, 1, 2, now=NOW+timedelta(minutes=4))
    state = lifecycle_state(session, 2, now=NOW+timedelta(minutes=4))
    assert state["attempts"] == 1 and state["deadline_at"] == initial["deadline_at"]
    assert before_attempt(session, 2, now=NOW+timedelta(minutes=5))
    after_error(session, 2, "ProviderTimeout", now=NOW+timedelta(minutes=5))
    state = lifecycle_state(session, 1, now=NOW+MATERIAL_TTL)
    assert state["state"] == "blocked"
    assert is_terminal_blocked(session, 2, now=NOW+MATERIAL_TTL)
    assert original.outcome["reason"] == SOURCE_REPLACED_REASON
    assert original.outcome["replacement_raw_item_id"] == 2


def test_read_only_guard_checks_expiry_without_any_write(session):
    lifecycle_state(session, 1, now=NOW)
    session.commit()
    record = session.scalar(select(SchedulerRun))
    original = copy.deepcopy(record.details)
    ensure_not_terminal(session, 1, now=NOW)
    with pytest.raises(LifecycleBlockedError):
        ensure_not_terminal(session, 1, now=NOW+MATERIAL_TTL)
    assert record.details == original and not session.dirty and not session.new


def test_orphan_alias_cannot_bypass_read_only_guard(session):
    lifecycle_state(session, 1, now=NOW)
    record = session.scalar(select(SchedulerRun))
    record.details = {"lifecycle_root_raw_id": 987, "raw_item_id": 1}
    session.commit()
    with pytest.raises(LifecycleBlockedError):
        ensure_not_terminal(session, 1, now=NOW)
    assert not can_retry_snapshot(session, 1, now=NOW)


def test_validated_refresh_updates_only_fingerprint_and_keeps_attempt_deadline(session):
    assert before_attempt(session, 1, now=NOW)
    after_error(session, 1, "SourceUnavailable", now=NOW)
    old = lifecycle_state(session, 1, now=NOW)
    session.get(RawItem, 1).body = "Обновлённые сведения официального источника"
    assert authorize_refreshed_evidence(session, 1, now=NOW+timedelta(minutes=1))
    state = lifecycle_state(session, 1, now=NOW+timedelta(minutes=1))
    assert state["attempts"] == old["attempts"]
    assert state["deadline_at"] == old["deadline_at"]
    assert state["next_attempt_at"] == old["next_attempt_at"]
    assert state["fingerprints"]["1"] != old["fingerprints"]["1"]


def test_stale_and_invalid_date_are_terminal_before_model_call(session):
    item = session.get(RawItem, 1)
    item.published_at = NOW-timedelta(days=10)
    assert not before_attempt(session, 1, now=NOW)
    assert lifecycle_state(session, 1, now=NOW)["state"] == "blocked"
    session.add(raw(2))
    session.flush()
    session.get(RawItem, 2).published_at_precision = "invalid"
    assert lifecycle_state(session, 2, now=NOW)["reason"] == "invalid_publication_time"


def test_empty_ranking_refusal_is_finite_and_does_not_crash_sweep(session):
    run = editorial(session)
    run.outcome = {**run.outcome, "ranking": None}
    result = sweep_materials(session, city="belgrade", now=NOW)
    assert result.rejection_audit_ids == (1,)
    assert lifecycle_state(session, 1, now=NOW+MATERIAL_TTL)["state"] == "blocked"


def test_unknown_state_fails_closed(session):
    lifecycle_state(session, 1, now=NOW)
    record = session.scalar(select(SchedulerRun))
    record.details = {**record.details, "next_action": "never_do_anything"}
    assert lifecycle_state(session, 1, now=NOW)["reason"] == "invalid_lifecycle_state"


def test_source_change_and_staleness_do_not_reopen_review(session):
    lifecycle_state(session, 1, now=NOW)
    session.get(RawItem, 1).body = "Изменённые исходные сведения"
    assert not before_attempt(session, 1, now=NOW)
    assert lifecycle_state(session, 1, now=NOW)["reason"] == "source_or_evidence_changed"


@pytest.mark.parametrize(
    "change", ["date", "already_blocked", "different_contents", "expired", "bad_snapshot"],
)
def test_visible_water_date_preserves_retry_only_if_the_other_evidence_matches(session, change):
    source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    sync_source(session, "belgrade", source)
    url = ("https://www.beograd.rs/lat/servisne-informacije/vodovod-i-kanalizacija/"
           "a116756/Kvarovi-na-vodovodnoj-mrezi-bice-otklonjeni-tokom-dana.html")
    html = (
        "<article><h1>Kvarovi na vodovodnoj mreži biće otklonjeni tokom dana</h1>"
        '<time class="story__time">19.11.2026.</time><p>Do 15.00</p>'
        "<p>Stari grad: Venizelosova 15</p></article>"
    )
    updated = replace(parse_beograd_article(html, source, url), source_html=html)
    old = replace(updated, body=updated.body.split("\n\n", 1)[1], source_html=None)
    assert capture_collected_batch(session, [old]) == (1, 0)
    item = session.scalar(select(RawItem).where(RawItem.url == url))
    assert item is not None
    raw_id = item.id
    assert before_attempt(session, raw_id, now=NOW)
    original = copy.deepcopy(after_error(session, raw_id, "StructuredOutputError", now=NOW).details)
    now = NOW + (MATERIAL_TTL if change == "expired" else timedelta(minutes=6))
    if change == "already_blocked":
        item.body = updated.body
        assert lifecycle_state(session, raw_id, now=now)["reason"] == "source_or_evidence_changed"
    if change == "different_contents":
        html = html.replace("Do 15.00", "Do 17.00")
        updated = replace(parse_beograd_article(html, source, url), source_html=html)
    assert capture_collected_batch(session, [updated]) == (1, 0)
    session.expire(item)
    if change == "bad_snapshot":
        snapshot = session.get(RawItemMediaSnapshot, raw_id)
        assert snapshot is not None
        snapshot.source_html_sha256 = "f" * 64
    sweep = sweep_materials(session, city="belgrade", now=now, raw_ids={raw_id})
    state = lifecycle_state(session, raw_id, now=now)
    assert state["deadline_at"] == original["deadline_at"]
    assert state["attempts"] == original["attempts"] == 1
    assert state["fallback_attempts"] == state["delivery_attempts"] == 0
    if change in {"date", "already_blocked"}:
        assert sweep.due_ids == (raw_id,)
        assert state["reason"] == "visible_publication_date_verified"
        assert state["fingerprints"][str(raw_id)] != original["fingerprints"][str(raw_id)]
    else:
        assert sweep.due_ids == ()
        assert state["state"] == "blocked"
    assert session.get(EditorialRun, raw_id) is None


def test_sweep_bulk_loads_5000_synthetic_items_without_n_plus_one(session):
    session.add_all(raw(raw_id) for raw_id in range(2, 5001))
    session.commit()
    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT"):
            statements.append(statement)

    event.listen(session.bind, "before_cursor_execute", count)
    try:
        first = sweep_materials(session, city="belgrade", now=NOW)
        session.commit()
        assert len(first.due_ids) == 5000
        assert len(statements) <= 4
        statements.clear()
        expired = sweep_materials(session, city="belgrade", now=NOW+MATERIAL_TTL)
        session.commit()
        assert len(expired.blocked_ids) == 5000
        assert len(statements) <= 4
    finally:
        event.remove(session.bind, "before_cursor_execute", count)
