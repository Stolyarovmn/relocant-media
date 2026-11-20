import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from facts_check_helpers import bind_facts_check
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_gpt_fallback_service import NOW, package_for_request
from test_gpt_fallback_service import fallback_session as service_session
from test_server_cycle import seeded as server_seeded

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument
from relocant_media.ai.package import EditorialPackage, canonical_json, context_sha256, draft_sha256
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import event_key
from relocant_media.domain.presentation import presentation_title
from relocant_media.ops import gpt_fallback_worker as worker
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import gpt_fallback
from relocant_media.services.editorial import _candidate, _snapshot_member
from relocant_media.services.editorial_products import build_presentation, present_draft
from relocant_media.services.gpt_batch_fallback import build_batch_request, build_ranked_request
from relocant_media.services.material_lifecycle import can_retry_snapshot, lifecycle_state
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

seeded = server_seeded
fallback_session = service_session


def test_primary_error_snapshot_is_archived_before_fallback_claim_without_resetting_budget(
    fallback_session, monkeypatch,
):
    session, raw, _ = fallback_session

    def forbidden(*args, **kwargs):
        pytest.fail("Фиксация резерва не должна обращаться к сети, модели или публикации")

    monkeypatch.setattr(worker.server_runtime, "fetch_documents", forbidden)
    monkeypatch.setattr(worker.server_cycle, "IsolatedGeminiProvider", forbidden)
    monkeypatch.setattr(worker.server_cycle, "publish_pending", forbidden)
    lifecycle_state(session, raw.id, now=NOW)
    ledger = session.scalar(select(SchedulerRun).where(
        SchedulerRun.task_type == "material_lifecycle"))
    ledger.details = {**ledger.details, "state": "waiting", "next_action": "retry_prepare",
                      "attempts": 1, "last_error_type": "TimeoutError"}
    source = session.get(Source, raw.source_id)
    evidence = _snapshot_member(raw, source)
    previous = EditorialRun(raw_item_id=raw.id, delivery_status="pending", created_at=NOW,
        evidence={**evidence, "documents": [evidence]},
        outcome={"status": "human_review", "reason": "selection_ungrounded_evidence",
                 "ranking": {"category": "transport"}})
    session.add(previous)
    session.commit()
    before = copy.deepcopy(ledger.details)
    old_outcome, old_evidence = copy.deepcopy(previous.outcome), copy.deepcopy(previous.evidence)
    assert "rejection_justified" not in before
    assert can_retry_snapshot(session, raw.id, now=NOW)
    request = gpt_fallback.build_request(session, now=NOW)
    assert request is not None
    gpt_fallback.validate_fallback_candidate(session, request, raw.id, now=NOW)

    assert worker._claim_fallback(session, raw.id, now=NOW)

    # Отдельная сессия видит committed budget до первого внешнего действия.
    with Session(session.get_bind()) as durable:
        saved = durable.get(SchedulerRun, ledger.id).details
        assert saved["state"] == "processing" and saved["attempt_kind"] == "fallback"
        assert saved["attempts"] == before["attempts"] == 1
        assert saved["fallback_attempts"] == before["fallback_attempts"] + 1 == 1
        for key in ("first_seen_at", "deadline_at", "fingerprint", "fingerprints",
                    "rejection_audits", "audit_attempts"):
            assert saved[key] == before[key]
        assert len(saved["attempt_records"]) == 1
        archived = saved["attempt_records"][0]
        assert archived["outcome"] == old_outcome and archived["evidence"] == old_evidence
        assert archived["created_at"] == NOW.isoformat()
        assert archived["archived_at"] == NOW.isoformat()
        assert durable.get(EditorialRun, raw.id) is None
        assert durable.get(RawItem, raw.id).body == raw.body
        assert durable.get(Source, raw.source_id).auto_publish == source.auto_publish

    # Повторная проверка исходного запроса остаётся действующей после архивирования.
    gpt_fallback.validate_fallback_candidate(session, request, raw.id, now=NOW)


def _input(package, request=None):
    request = request or {"version": 1, "private": "PRIVATE-REQUEST-SENTINEL"}
    return {
        "request": request,
        "result": {"version": 1,
                   "request_sha256": hashlib.sha256(canonical_json(request).encode()).hexdigest(),
                   "packages": [package.model_dump(mode="json")]},
    }


def _parse(value):
    return worker.FallbackInput.model_validate_json(json.dumps(value))


@pytest.fixture
def ready_package(seeded):
    engine, raw_id, provider = seeded
    profile = load_city_profile("belgrade")
    with Session(engine) as session:
        raw = session.get(RawItem, raw_id)
        source = session.get(Source, raw.source_id)
        source.auto_publish = True
        session.commit()
        evidence = _snapshot_member(raw, source)
        document = EvidenceDocument.model_validate(evidence)
        presentation = build_presentation(
            session, raw_id, (document,), document_ids=[raw_id],
            profile=profile, rubric_id="city_changes",
        )
        draft = provider._responses["draft"].model_copy(update={
            "title": presentation_title(provider._responses["draft"].title),
        })
        draft = present_draft(draft, presentation, category=provider._responses["rank"].category)
        outcome = EditorialOutcome(
            status="ready", reason="verified", confidence=.99,
            ranking=provider._responses["rank"], claims=provider._responses["claims"],
            verification=provider._responses["verify"], draft=draft,
        )
        values = {
            "version": 2, "policy_version": "subscription-editorial-v2", "city": source.city,
            "raw_item_id": raw_id, "source_id": raw.source_id, "article_id": raw.external_id,
            "evidence": document.model_dump(mode="json"),
            "event": {"event_key": event_key(_candidate(raw)),
                      "editorial_rule_version": "belgrade-editorial-v2", "documents": [evidence]},
            "presentation": presentation.model_dump(mode="json"),
            "outcome": outcome.model_dump(mode="json"),
            "reviews": [{"reviewer_id": f"subscription-gpt-{role}", "role": role,
                         "source_id": raw.source_id, "article_id": raw.external_id,
                         "reviewed_at": datetime.now(UTC).isoformat(), "verdict": "approve",
                         "score": 94, "flags": [], "live_source_checked": True,
                         "draft_sha256": draft_sha256(draft), "context_sha256": "0" * 64}
                        for role in ("facts", "utility")],
        }
    bind_facts_check(values)
    package = EditorialPackage.model_validate_json(json.dumps(values))
    digest = context_sha256(package)
    package = package.model_copy(update={"reviews": tuple(
        review.model_copy(update={"context_sha256": digest}) for review in package.reviews
    )})
    return engine, package


@pytest.fixture
def offline(monkeypatch):
    calls = []

    def validate(session, request, package, *, now):
        calls.append("validate")
        assert now.tzinfo is not None
        return package

    def live(package):
        calls.append("live")
        return tuple(EvidenceDocument.model_validate(
            document.model_dump(include=set(EvidenceDocument.model_fields)))
            for document in package.event.documents)

    def forbidden(*args, **kwargs):
        raise AssertionError("ИИ, intake и обратная связь не входят в GPT fallback")

    monkeypatch.setattr(worker, "validate_fallback_package", validate)
    monkeypatch.setattr(worker.server_runtime, "fetch_documents", live)
    monkeypatch.setattr(worker.server_cycle, "IsolatedGeminiProvider", forbidden)
    monkeypatch.setattr(worker.server_cycle, "receive_private_tips", forbidden)
    monkeypatch.setattr(worker.server_cycle, "reply_to_submissions", forbidden)
    return calls


@pytest.mark.parametrize("mutation", ["hash", "duplicate", "empty", "version", "unknown"])
def test_input_contract_is_strict_and_binds_the_request(ready_package, mutation):
    _, package = ready_package
    payload = _input(package)
    if mutation == "hash":
        payload["result"]["request_sha256"] = "0" * 64
    elif mutation == "duplicate":
        payload["result"]["packages"] *= 2
    elif mutation == "empty":
        payload["result"]["packages"] = []
    elif mutation == "version":
        payload["result"]["version"] = True
    else:
        payload["result"]["skip_validation"] = True
    with pytest.raises(ValidationError):
        _parse(payload)


def test_worker_revalidates_live_evidence_then_uses_normal_import(ready_package, offline):
    engine, package = ready_package
    result = worker.run_worker(engine, _parse(_input(package)))
    assert offline == ["validate", "live", "validate"]
    assert result["status"] == "PRIMARY_OK"
    assert result["imported"] == result["ready"] == 1
    assert result["sent_count"] == result["model_calls"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, package.raw_item_id)
        assert run.outcome["_package"] == package.model_dump(mode="json")
        assert run.delivery_status == "pending"
        assert session.get(Post, run.post_id).body == package.outcome.draft.body
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["changed", "missing", "map_error"])
def test_changed_live_evidence_or_map_never_reaches_import(
    ready_package, offline, monkeypatch, mutation,
):
    engine, package = ready_package

    def live(package):
        if mutation == "map_error":
            raise ValueError("PRIVATE-MAP-URL")
        if mutation == "missing":
            return ()
        return (package.evidence.model_copy(update={"body": "Изменённый текст"}),)

    monkeypatch.setattr(worker.server_runtime, "fetch_documents", live)
    result = worker.run_worker(engine, _parse(_input(package)), send_enabled=True)
    assert offline == ["validate"]
    assert result["status"] == "PRIMARY_FAILED"
    assert result["processing_errors"] == [{"stage": "live", "error_type": "ValueError"}]
    assert "PRIVATE" not in json.dumps(result)
    with Session(engine) as session:
        assert session.get(EditorialRun, package.raw_item_id) is None
        assert session.scalar(select(Post)) is None


def test_revalidation_failure_prevents_import_after_successful_live_fetch(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    validated = []

    def validate(session, request, package, *, now):
        validated.append(True)
        if len(validated) == 2:
            raise ValueError("PRIVATE-CHANGED-SCHEDULER")
        return package

    monkeypatch.setattr(worker, "validate_fallback_package", validate)
    result = worker.run_worker(engine, _parse(_input(package)))
    assert validated == [True, True]
    assert result["processing_errors"] == [{"stage": "revalidate", "error_type": "ValueError"}]
    with Session(engine) as session:
        assert session.get(EditorialRun, package.raw_item_id) is None


@pytest.mark.parametrize("invalidate", [False, True])
def test_real_fallback_validation_requires_the_recorded_gemini_failure(
    fallback_session, monkeypatch, invalidate,
):
    session, raw, scheduler = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    fetched = []
    if invalidate:
        scheduler.details = {"processing_errors": [
            {"raw_item_id": raw.id, "error_type": "ValueError"},
        ]}
        session.commit()

    def live(value):
        fetched.append(True)
        return tuple(EvidenceDocument.model_validate(document.model_dump())
                     for document in value.event.documents)

    monkeypatch.setattr(worker.server_runtime, "fetch_documents", live)
    result = worker.run_worker(session.get_bind(), _parse(_input(package, request)), now=NOW)
    assert result["imported"] == (0 if invalidate else 1)
    assert result["fallback_required"] is invalidate
    assert fetched == ([] if invalidate else [True])
    assert (session.get(EditorialRun, raw.id) is None) is invalidate


@pytest.mark.parametrize("approve", [True, False])
def test_batch_reserve_reaches_final_decision_in_same_cycle(fallback_session, monkeypatch, approve):
    session, raw, scheduler = fallback_session
    engine = session.get_bind()
    scheduler.details = {"processing_errors": [], "batch_preselection": {
        "status": "gpt_waiting", "gpt_slots": 5, "gpt_candidate_ids": [raw.id],
        "error_type": "GeminiWorkerError", "error_stage": "provider",
        "full_review_ids": [],
    }}
    session.commit()
    brief = build_batch_request(session, now=NOW)
    rank = {"version": 2, "kind": "batch_preselection", "selected_ids": [raw.id],
            "request_sha256": hashlib.sha256(canonical_json(brief).encode()).hexdigest()}
    assert worker.save_batch_rank(engine, brief, rank, now=NOW)["selected_count"] == 1
    assert worker.save_batch_rank(engine, brief, rank, now=NOW)["selected_count"] == 1
    request = build_ranked_request(session, brief, rank, now=NOW)
    assert request["scheduler_run_id"] == scheduler.id
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == {raw.id}
    if approve:
        payload = _input(package_for_request(request), request)
        monkeypatch.setattr(worker, "_check_live_evidence", lambda package: None)
    else:
        payload = {"request": request, "result": {
            "version": 1,
            "request_sha256": hashlib.sha256(canonical_json(request).encode()).hexdigest(),
            "packages": [],
            "rejected": [{"raw_item_id": raw.id, "category": "transport",
                          "rationale": "Детали заявки уже не представляют интереса читателю."}],
        }}
    result = worker.run_worker(engine, _parse(payload), now=NOW)
    assert result["status"] == "PRIMARY_OK"
    assert result["imported"] == int(approve)
    assert result["declined"] == int(not approve)
    assert result["sent_count"] == 0
    assert result["fallback_required"] is False
    run = session.get(EditorialRun, raw.id)
    assert run is not None
    if not approve:
        assert run.post_id is None
        assert run.outcome["reason"] == "lifecycle_fallback_editorial_rejection"
        assert run.outcome["_lifecycle_block"]["previous_reason"] == "gpt_ranking_rejected"
    assert build_ranked_request(session, brief, rank, now=NOW) is None
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()


@pytest.mark.parametrize("audit_failure", ["none", "both", "second"])
@pytest.mark.parametrize("approve", [False, True])
def test_unconfirmed_rejection_uses_existing_full_fallback_once(
    fallback_session, monkeypatch, approve, audit_failure,
):
    from relocant_media.services import material_lifecycle as core

    session, raw, scheduler = fallback_session
    source = session.get(Source, raw.source_id)
    snapshot = _snapshot_member(raw, source)
    now = NOW
    original = core.lifecycle_state(session, raw.id, now=now)

    def reject():
        session.add(EditorialRun(raw_item_id=raw.id, delivery_status="pending", created_at=now,
            evidence={**snapshot, "documents": [snapshot]},
            outcome={"status": "skipped", "reason": "not_selected",
                     "ranking": {"category": "transport"}}))
        session.flush()
        core.after_result(session, raw.id, now=now)

    assert core.before_attempt(session, raw.id, now=now)
    reject()
    for attempt in range(2):
        assert core.before_attempt(session, raw.id, now=now, channel="rejection_audit")
        if audit_failure == "both" or audit_failure == "second" and attempt == 1:
            core.after_error(session, raw.id, "TimeoutError", now=now, channel="rejection_audit")
            if attempt == 0:
                now += timedelta(minutes=5)
        else:
            core.record_rejection_review(session, raw.id, {
                "decision": "retry_processing", "basis": "unsupported_rejection",
                "reviewed_at": now.isoformat(), "synthetic_attempt": attempt,
            }, now=now)
            core.rejection_reviewed(session, raw.id, justified=False, now=now)
            if attempt == 0:
                assert core.before_attempt(session, raw.id, now=now)
                assert core.archive_for_retry(session, raw.id, now=now)
                reject()
    before = core.lifecycle_state(session, raw.id, now=now)
    assert before["next_action"] == "fallback" and before["audit_attempts"] == 2
    assert before["fallback_attempts"] == 0
    if audit_failure != "none":
        assert before["rejection_justified"] is None
        assert before["unconfirmed_rejection_fallback"] is True
    if audit_failure == "second":
        assert [review["decision"] for review in before["rejection_review_history"]] == [
            "retry_processing", "audit_error"]
    scheduler.details = {"processing_errors": [], "lifecycle_fallback_ids": [raw.id]}
    session.commit()
    request = gpt_fallback.build_request(session, now=now)
    assert request is not None and request["scheduler_run_id"] == scheduler.id
    gpt_fallback.validate_fallback_candidate(session, request, raw.id, now=now)
    if approve:
        payload = _input(package_for_request(request), request)
        monkeypatch.setattr(worker, "_check_live_evidence", lambda package: None)
    else:
        payload = {"request": request, "result": {"version": 1,
            "request_sha256": hashlib.sha256(canonical_json(request).encode()).hexdigest(),
            "packages": [], "rejected": [{"raw_item_id": raw.id, "category": "transport",
                "rationale": "Исходные сведения не подтверждают значимость события."}]}}
    result = worker.run_worker(session.get_bind(), _parse(payload), now=now)
    assert result["unresolved"] == 0 and result["sent_count"] == 0
    session.expire_all()
    state = core.lifecycle_state(session, raw.id, now=now)
    assert state["fallback_attempts"] == 1 and state["audit_attempts"] == 2
    assert state["attempts"] == before["attempts"]
    assert state["deadline_at"] == original["deadline_at"]
    assert state["fallback_deadline_at"] == before["fallback_deadline_at"]
    assert len(state["rejection_review_history"]) == 2
    if approve:
        assert state["state"] == "ready" and state["next_action"] == "deliver"
        assert session.get(EditorialRun, raw.id).post_id is not None
    else:
        assert state["state"] == "blocked" and state["reason"] == "fallback_editorial_rejection"
        assert session.get(EditorialRun, raw.id).post_id is None
    assert not core.before_attempt(session, raw.id, now=now, channel="fallback")


def test_real_gemini_failure_can_recover_an_existing_selection_hold(
    fallback_session, monkeypatch,
):
    session, raw, _ = fallback_session
    source = session.get(Source, raw.source_id)
    document = EvidenceDocument.model_validate(_snapshot_member(raw, source))
    hold = EditorialRun(
        raw_item_id=raw.id, delivery_status="pending",
        outcome={"status": "human_review", "reason": "selection_ungrounded_evidence"},
        evidence={**document.model_dump(mode="json"), "city": source.city,
                  "event_key": event_key(_candidate(raw)),
                  "documents": [_snapshot_member(raw, source)]},
    )
    session.add(hold)
    session.commit()
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == {raw.id}
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    monkeypatch.setattr(worker, "_check_live_evidence", lambda package: None)
    result = worker.run_worker(session.get_bind(), _parse(_input(package, request)), now=NOW)
    assert result["imported"] == 1
    assert result["unresolved"] == 0
    session.refresh(hold)
    assert hold.outcome["_previous_policy_hold"]["outcome"]["reason"] == (
        "selection_ungrounded_evidence"
    )
    assert hold.outcome["_autonomous_assessment"]["reviewed_package"]
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()


def test_gpt_fallback_does_not_reopen_a_hold_with_a_delivery_receipt(fallback_session):
    session, raw, _ = fallback_session
    hold = EditorialRun(
        raw_item_id=raw.id, delivery_status="pending", staging_message_id=42,
        outcome={"status": "human_review", "reason": "selection_ungrounded_evidence"},
        evidence={},
    )
    session.add(hold)
    session.commit()
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()
    assert gpt_fallback.build_request(session, now=NOW) is None


@pytest.mark.parametrize("existing_hold", [False, True])
def test_subset_response_keeps_unresolved_tail_but_accepts_a_known_human_hold(
    ready_package, offline, existing_hold,
):
    engine, package = ready_package
    with Session(engine) as session:
        other = RawItem(
            source_id=package.source_id, external_id="unanswered", url="https://example.com/other",
            title="Самостоятельная тема", body="Другой материал из запроса", published_at=None,
        )
        session.add(other)
        session.flush()
        other_id = other.id
        if existing_hold:
            session.add(EditorialRun(raw_item_id=other_id, evidence={},
                                     outcome={"status": "human_review"}, delivery_status="pending"))
        session.commit()
    request = {"candidates": [{"raw_item_id": package.raw_item_id}, {"raw_item_id": other_id}]}
    result = worker.run_worker(engine, _parse(_input(package, request)))
    assert result["unresolved"] == (0 if existing_hold else 1)
    assert result["fallback_required"] is not existing_hold
    assert result["processing_errors"] == []


@pytest.mark.parametrize("kind", ["source_map", "native_map"])
def test_government_map_fails_closed_until_its_live_adapter_checks_html(
    ready_package, offline, kind,
):
    _, package = ready_package
    document = package.event.documents[0].model_copy(update={
        "source_id": worker.server_runtime.SOURCE_ID,
    })
    additions = ({"source_map": {"source_page": document.url}}
                 if kind == "source_map" else {"native_map": {"point": {
                     "source_url": document.url}}})
    package = package.model_copy(update={
        "event": package.event.model_copy(update={"documents": (document,)}),
        "presentation": package.presentation.model_copy(update=additions),
    })
    with pytest.raises(ValueError, match="живую проверку карты"):
        worker._check_live_evidence(package)
    assert offline == []


def test_only_successfully_imported_packages_enter_normal_delivery(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    payload = _input(package)
    other = package.model_dump(mode="json")
    other["raw_item_id"] = package.raw_item_id + 100
    other["event"]["documents"][0]["raw_item_id"] = other["raw_item_id"]
    payload["result"]["packages"].append(other)
    sent = []
    original_fetch = worker.server_cycle.fetch_documents

    def validate(session, request, value, *, now):
        if value.raw_item_id != package.raw_item_id:
            raise ValueError("PRIVATE-UNREQUESTED-CANDIDATE")
        return value

    async def publish(engine, *, enabled, only_raw_ids, blocked):
        assert enabled is True
        assert worker.server_cycle.fetch_documents is worker.server_runtime.fetch_documents
        sent.append(only_raw_ids)
        return [42]

    monkeypatch.setattr(worker, "validate_fallback_package", validate)
    monkeypatch.setattr(worker.server_cycle, "publish_pending", publish)
    result = worker.run_worker(engine, _parse(payload), send_enabled=True)
    assert result["status"] == "PRIMARY_PARTIAL"
    assert result["imported"] == result["rejected"] == result["sent_count"] == 1
    assert sent == [{package.raw_item_id}]
    assert worker.server_cycle.fetch_documents is original_fetch
    assert "PRIVATE" not in json.dumps(result)


def test_exact_sent_replay_preserves_receipt_without_entering_delivery(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    payload = _parse(_input(package))
    assert worker.run_worker(engine, payload)["imported"] == 1
    with Session(engine) as session:
        run = session.get(EditorialRun, package.raw_item_id)
        run.delivery_status = "sent"
        post = session.get(Post, run.post_id)
        post.telegram_message_id = 42
        post.published_at = datetime.now(UTC)
        session.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Подтверждённая квитанция не должна запускать повторную отправку")

    monkeypatch.setattr(worker.server_cycle, "publish_pending", forbidden)
    result = worker.run_worker(engine, payload, send_enabled=True)
    assert result["status"] == "PRIMARY_OK" and result["sent_count"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, package.raw_item_id)
        assert run.delivery_status == "sent"
        assert session.get(Post, run.post_id).telegram_message_id == 42


def test_delivery_failure_restores_fetch_adapter_and_does_not_retry(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    previous = worker.server_cycle.fetch_documents
    attempts = []

    async def failed(*args, **kwargs):
        attempts.append(True)
        raise TimeoutError("PRIVATE-SEND-DETAILS")

    monkeypatch.setattr(worker.server_cycle, "publish_pending", failed)
    result = worker.run_worker(engine, _parse(_input(package)), send_enabled=True)
    assert attempts == [True]
    assert result["status"] == "PRIMARY_PARTIAL"
    assert result["processing_errors"] == [{"stage": "publish", "error_type": "TimeoutError"}]
    assert worker.server_cycle.fetch_documents is previous
    assert "PRIVATE" not in json.dumps(result)


def test_cli_uses_existing_database_and_writes_only_structural_report(
    ready_package, offline, tmp_path, monkeypatch, capsys,
):
    engine, package = ready_package
    private = tmp_path / "private.json"
    private.write_text(json.dumps(_input(package)), encoding="utf-8")
    output = tmp_path / "new-cycle"
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    monkeypatch.delenv("EDITORIAL_SEND_ENABLED", raising=False)
    monkeypatch.setattr(sys, "argv", ["worker", "--input", str(private), "--output", str(output)])

    worker.main()

    printed = capsys.readouterr().out
    report = json.loads((output / "server-report.json").read_text(encoding="utf-8"))
    assert json.loads(printed) == report
    assert report["status"] == "PRIMARY_OK" and report["delivery_enabled"] is False
    assert "PRIVATE" not in printed and package.evidence.url not in printed
    assert package.outcome.draft.body not in printed
    assert [item.name for item in output.iterdir()] == ["server-report.json"]
    before = (output / "server-report.json").read_bytes()
    with pytest.raises(SystemExit):
        worker.main()
    assert (output / "server-report.json").read_bytes() == before


def test_invalid_input_does_not_open_database_or_echo_private_data(tmp_path, monkeypatch, capsys):
    private = tmp_path / "private.json"
    private.write_text('{"PRIVATE-INVALID": "private-input"}', encoding="utf-8")
    output = tmp_path / "new-cycle"
    monkeypatch.setattr(sys, "argv", ["worker", "--input", str(private), "--output", str(output)])
    monkeypatch.setattr(worker, "create_engine", lambda *args: pytest.fail("no database needed"))
    with pytest.raises(SystemExit):
        worker.main()
    printed = capsys.readouterr().out
    assert json.loads(printed)["error_type"] == "ValidationError"
    assert "PRIVATE" not in printed and "private-input" not in printed
