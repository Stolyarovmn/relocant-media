"""Ранний отказ без rank проверяется по событию, а не отправляется в LLM-аудит."""

import copy
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy.orm import Session
from test_dedup import single_route_detour_reprint
from test_editorial_package import bind_context, package_payload
from test_material_lifecycle_runtime import BODY, NOW
from test_material_lifecycle_runtime import engine as _engine

from relocant_media.ai.models import EditorialOutcome
from relocant_media.ai.package import canonical_json, draft_sha256
from relocant_media.domain.dedup import same_event
from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.services import material_lifecycle as core
from relocant_media.services.editorial import (
    EditorialService,
    _candidate,
    _document,
    _reviewed_event_item,
    _snapshot_member,
    reviewed_event_proof,
)
from relocant_media.services.material_lifecycle_runtime import MaterialLifecycleRuntime
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission

engine = _engine


class NoModels:
    def generate_json(self, **kwargs):
        pytest.fail("Детерминированный отказ нельзя проверять моделью")


def outcome(document):
    return EditorialOutcome.model_validate({
        "status":"ready", "reason":"verified", "confidence":0.95,
        "ranking":{"should_process":True, "category":"transport", "relevance":0.95,
                   "confidence":0.95, "rationale":"Подтверждено предупреждение пассажирам."},
        "claims":{"claims":[{"claim_id":"c1", "text":"Авиакомпания предупредила пассажиров.",
            "material":True, "citations":[{"source_id":document.source_id,
                "url":document.url, "quote":document.body}]}]},
        "verification":{"claims":[{"claim_id":"c1", "supported":True, "confidence":0.95,
                                    "reason":"Факт прямо указан в исходном документе."}]},
        "draft":{"title":"Предупреждение для пассажиров", "body":"Авиакомпания сообщила "
                 "о возможных задержках. Проверяйте информацию о поездке у перевозчика.",
                 "claim_ids":["c1"]}})


def seed_duplicate(engine, *, peer=False, body=BODY):
    with Session(engine) as session:
        source = session.get(Source, "official")
        source.ownership = "official"
        if peer:
            source = Source(id="peer", city="belgrade", name="Другой публичный источник",
                url="https://peer.example", source_type="official_news", trust=1,
                ownership="official", enabled=True, auto_publish=True, high_risk=False)
            session.add(source)
            session.flush()
        anchor = session.get(RawItem, 1)
        target = RawItem(id=2, source_id=source.id, external_id="2",
            url=f"{source.url}/original/", title=anchor.title, body=body,
            published_at=anchor.published_at, published_at_precision="instant", ingested_at=NOW)
        session.add(target)
        session.flush()
        snapshot = _snapshot_member(target, source)
        reviewed = outcome(_document(target))
        post = Post(city="belgrade", category="transport", status="ready",
                    title=reviewed.draft.title, body=reviewed.draft.body, confidence=0.95)
        session.add(post)
        session.flush()
        session.add(EditorialRun(raw_item_id=2, post_id=post.id, created_at=NOW,
            delivery_status="pending", outcome=reviewed.model_dump(mode="json"),
            evidence={**snapshot, "city":"belgrade", "documents":[snapshot]}))
        # Дословная форма исторической ранней ветки _prepare_reviewed.
        session.add(EditorialRun(raw_item_id=1, created_at=NOW, delivery_status="pending",
            outcome={"status":"skipped", "reason":"event_already_reviewed",
                     "duplicate_of_raw_item":2, "ai_calls":0},
            evidence={"raw_item_id":1, "source_id":"official"}))
        session.commit()


def proof(session):
    anchor, target = session.get(RawItem, 1), session.get(RawItem, 2)
    return reviewed_event_proof(session, anchor=anchor,
        anchor_source=session.get(Source, anchor.source_id), target=target, now=NOW)


def test_exact_early_skip_is_confirmed_without_rank_documents_or_model(engine):
    seed_duplicate(engine)
    runtime = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW)
    result = runtime.process(1, lambda _: pytest.fail("Настоящий дубль не обрабатывается снова"))
    state = result["lifecycle"]
    assert state["state"] == "blocked" and state["reason"] == "event_duplicate_confirmed"
    assert state["audit_attempts"] == state["rejection_audits"] == 1
    assert state["attempts"] == 0 and state["rejection_justified"] is True
    assert state["rejection_review"]["kind"] == "deterministic_event"
    assert len(state["rejection_review"]["context_sha256"]) == 64


def test_short_cross_source_reprint_keeps_strong_body_proof(engine):
    seed_duplicate(engine, peer=True, body="Bez vode ce biti deo Zvezdare od 08 do 18 casova.")
    with Session(engine) as session:
        session.get(RawItem, 1).body = "Deo Zvezdare ce biti bez vode od 08 do 18 casova."
        session.commit()
        assert proof(session).kind == "reviewed"


@pytest.mark.parametrize(("case", "expected"), [
    ("confirmed", "published"),
    ("uncertain", "delivery_reconciliation"),
    ("changed_date", None),
    ("unknown_host", None),
])
def test_official_detour_proof_respects_the_receipt_and_material_changes(engine, case, expected):
    seed_duplicate(engine)
    original, reprint = single_route_detour_reprint()
    with Session(engine) as session:
        source = Source(id=original.source_id, city="belgrade", name="Город Белград",
            url="https://www.beograd.rs", source_type="official_news", trust=1,
            ownership="official", enabled=True, auto_publish=True, high_risk=False)
        session.add(source)
        session.flush()
        anchor, target = session.get(RawItem, 1), session.get(RawItem, 2)
        for item, notice in ((anchor, reprint), (target, original)):
            item.source_id, item.url, item.title = source.id, notice.url, notice.title
            item.body, item.published_at = notice.body, notice.published_at
            item.published_at_precision = "day"
        if case == "changed_date":
            anchor.body = anchor.body.replace("11. decembra", "12. decembra")
        if case == "unknown_host":
            anchor.url = anchor.url.replace("www.beograd.rs", "example.com")
        run = session.get(EditorialRun, target.id)
        snapshot = _snapshot_member(target, source)
        run.evidence = {**snapshot, "city": source.city, "documents": [snapshot]}
        if case == "uncertain":
            run.delivery_status = "uncertain"
        else:
            run.delivery_status = "sent"
            session.get(Post, run.post_id).telegram_message_id = 124
        session.commit()

        match = proof(session)
        assert (match.kind if match is not None else None) == expected


@pytest.mark.parametrize("transport", [False, True])
def test_fresh_duplicate_is_settled_before_selector_without_fake_attempt(engine, transport):
    seed_duplicate(engine)
    with Session(engine) as session:
        session.delete(session.get(EditorialRun, 1))
        if transport:
            target = session.get(EditorialRun, 2)
            target.delivery_status = "uncertain"
            core.lifecycle_state(session, 2, now=NOW)
        session.commit()
    runtime = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW)
    sweep = runtime.reconcile()
    assert 1 in sweep.blocked_ids and 1 in sweep.excluded_ids
    with Session(engine) as session:
        state = core.lifecycle_state(session, 1, now=NOW)
        assert state["reason"] == ("related_delivery_reconciliation_required" if transport
                                   else "event_duplicate_confirmed")
        assert state["rejection_review"]["kind"] == "deterministic_event"
        assert state["rejection_review"]["basis"] == ("delivery_reconciliation" if transport
                                                    else "reviewed")
        assert all(state[key] == 0 for key in ("attempts", "fallback_attempts", "audit_attempts"))
        assert state["deadline_at"] == (NOW+core.MATERIAL_TTL).isoformat()
        assert session.get(EditorialRun, 1) is None
        manifest = select_candidates(session, city="belgrade", since=NOW-timedelta(hours=2),
            until=NOW, limit=5, allowed_raw_ids={1}, excluded_raw_ids=set(sweep.excluded_ids))
        assert manifest["candidate_count"] == manifest["eligible_count"] == 0
        if transport:
            assert session.get(EditorialRun, 2).delivery_status == "uncertain"


@pytest.mark.parametrize("reason", ["skipped", "owner_closed"])
def test_sweep_does_not_settle_fresh_news_against_archived_rejection(engine, reason):
    seed_duplicate(engine)
    with Session(engine) as session:
        session.delete(session.get(EditorialRun, 1))
        if reason == "skipped":
            target = session.get(EditorialRun, 2)
            target.outcome, target.evidence = {"status":"skipped", "reason":"not_selected"}, {}
        else:
            core.close_by_owner(session, 2, now=NOW)
        session.commit()
    sweep = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW).reconcile()
    assert 1 not in sweep.blocked_ids and 1 not in sweep.excluded_ids
    with Session(engine) as session:
        state = core.lifecycle_state(session, 1, now=NOW)
        assert state["state"] == "waiting" and state["next_action"] == "prepare"
        assert "rejection_review" not in state
        manifest = select_candidates(session, city="belgrade", since=NOW-timedelta(hours=2),
            until=NOW, limit=5, allowed_raw_ids={1}, excluded_raw_ids=set(sweep.excluded_ids))
        assert manifest["candidate_count"] == 1


@pytest.mark.parametrize("damage", ["missing_target", "empty_evidence", "changed_source",
    "old_skipped", "owner_closed", "owner_closed_approved", "expired_context", "extra_verdict",
    "title_only", "target_policy_changed", "missing_post"])
def test_unproved_early_skip_gets_one_ordinary_retry_without_fake_rejection(engine, damage):
    seed_duplicate(engine, peer=damage == "target_policy_changed")
    with Session(engine) as session:
        target = session.get(EditorialRun, 2)
        if damage == "missing_target":
            session.delete(target)
            session.flush()
            session.delete(session.get(RawItem, 2))
        elif damage == "empty_evidence":
            target.evidence = {"raw_item_id":2, "source_id":"official"}
        elif damage == "missing_post":
            target.post_id = None
        elif damage == "changed_source":
            session.get(RawItem, 2).body = "Источник опубликовал совершенно другое сообщение."
        elif damage == "old_skipped":
            target.outcome, target.evidence = {"status":"skipped"}, {}
        elif damage in {"owner_closed", "owner_closed_approved"}:
            if damage == "owner_closed_approved":
                target.outcome = {**target.outcome, "_package_sha256":"a"*64}
                session.add(Submission(kind="owner_editorial_review", status="approved",
                    payload={"raw_item_id":2, "package_sha256":"a"*64}))
                session.flush()
            core.close_by_owner(session, 2, now=NOW)
        elif damage == "expired_context":
            target.created_at = NOW-timedelta(hours=7)
        elif damage == "target_policy_changed":
            session.get(Source, "peer").high_risk = True
        elif damage == "extra_verdict":
            old = copy.deepcopy(target.outcome)
            old["verification"]["claims"].append({"claim_id":"c999", "supported":True,
                "confidence":1, "reason":"Это лишний непроверенный идентификатор факта."})
            old["draft"]["claim_ids"] = ["c999"]
            target.outcome = old
        else:
            item, source = session.get(RawItem, 2), session.get(Source, "official")
            item.body = "Участники выставки показали новые рисунки в пространстве галереи."
            snapshot = _snapshot_member(item, source)
            target.evidence = {**snapshot, "city":"belgrade", "documents":[snapshot]}
            updated = outcome(_document(item))
            target.outcome = updated.model_dump(mode="json")
        session.commit()
    runtime = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW)
    first = runtime.process(1, lambda _: pytest.fail("Retry отделён от аудита"),
                            retry_refuted=False)
    state = first["lifecycle"]
    assert state["next_action"] == "retry_prepare" and not state["rejection_justified"]
    assert state["audit_attempts"] == 1 and state["attempts"] == 0
    assert state["reason"] != "rejection_confirmed"
    budget = {key: state[key] for key in ("deadline_at", "first_seen_at", "fingerprints")}
    calls = []

    class OrdinaryPipeline:
        def run(self, *, city, documents):
            calls.append((city, documents))
            return outcome(documents[0])

    def delegate(raw_id):
        with Session(engine) as session:
            anchor = session.get(RawItem, raw_id)
            assert _reviewed_event_item(session, anchor=anchor,
                anchor_source=session.get(Source, anchor.source_id)) is None
            run = EditorialService(session, OrdinaryPipeline()).prepare(raw_id)
            return {"raw_item_id":raw_id, "status":run.outcome["status"]}

    result = runtime.process(1, delegate)
    assert len(calls) == 1 and result["lifecycle"]["attempts"] == 1
    assert {key: result["lifecycle"][key] for key in budget} == budget
    archive = result["lifecycle"]["attempt_records"]
    assert archive[0]["outcome"]["reason"] == "event_already_reviewed"
    assert "ranking" not in archive[0]["outcome"]
    with Session(engine) as session:
        fresh = session.get(EditorialRun, 1)
        assert fresh.outcome["reason"] != "event_already_reviewed"
        assert "_package" not in fresh.outcome  # Эта подготовка сама по себе не разрешает отправку.
        assert fresh.delivery_status == "pending"
        if damage == "owner_closed_approved":
            assert session.query(Submission).one().status == "approved"
        if damage == "target_policy_changed":
            assert session.get(Source, "peer").high_risk
            assert fresh.outcome["status"] == "human_review"


@pytest.mark.parametrize("transport", ["sending", "uncertain", "staging", "sent_no_receipt"])
def test_unknown_target_send_blocks_reconciliation_and_preserves_target(engine, transport):
    seed_duplicate(engine)
    with Session(engine) as session:
        run = session.get(EditorialRun, 2)
        if transport == "staging":
            run.staging_message_id = 51
        else:
            run.delivery_status = "sent" if transport == "sent_no_receipt" else transport
        saved = copy.deepcopy(run.outcome), copy.deepcopy(run.evidence), run.delivery_status
        session.commit()
    result = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW).process(
        1, lambda _: pytest.fail("Нельзя повторять неизвестную отправку"))
    assert result["reason"] == "related_delivery_reconciliation_required"
    assert result["lifecycle"]["rejection_review"]["decision"] == "delivery_reconciliation"
    assert "rejection_justified" not in result["lifecycle"]
    with Session(engine) as session:
        run = session.get(EditorialRun, 2)
        assert (run.outcome, run.evidence, run.delivery_status) == saved


def test_published_snapshot_still_blocks_duplicate_after_current_raw_changes(engine):
    seed_duplicate(engine)
    with Session(engine) as session:
        run = session.get(EditorialRun, 2)
        run.delivery_status = "sent"
        post = session.get(Post, run.post_id)
        post.telegram_message_id, post.published_at = 123, NOW
        target = session.get(RawItem, 2)
        target.title, target.body, target.url = "Другой повод", "Новый отдельный текст.", \
            "https://example.test/replaced/"
        session.commit()
        assert proof(session).kind == "published"
        assert _reviewed_event_item(session, anchor=session.get(RawItem, 1),
            anchor_source=session.get(Source, "official")).id == 2
    result = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW).process(
        1, lambda _: pytest.fail("Квитанция опубликованного события не открывает повтор"))
    assert result["reason"] == "event_duplicate_confirmed"
    with Session(engine) as session:
        run = session.get(EditorialRun, 2)
        assert run.delivery_status == "sent"
        assert session.get(Post, run.post_id).telegram_message_id == 123


def test_weak_saved_publication_match_requires_reconciliation_after_raw_changed(engine):
    old = ("U Beogradu je otvorena nova galerija koja predstavlja savremene umetnike "
           "i njihove radove.")
    seed_duplicate(engine, peer=True, body=old)
    with Session(engine) as session:
        anchor = session.get(RawItem, 1)
        anchor.body = ("Beograd dobija prostor za umetnicke postavke gde publika vidi "
                       "nove kolekcije.")
        run = session.get(EditorialRun, 2)
        run.delivery_status = "sent"
        session.get(Post, run.post_id).telegram_message_id = 123
        target = session.get(RawItem, 2)
        assert same_event(_candidate(anchor), _candidate(target))
        target.title, target.body = "Другой повод", "Это уже иной текущий снимок источника."
        target.url = "https://peer.example/changed/"
        session.commit()
        assert proof(session).kind == "delivery_reconciliation"
    result = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW).process(
        1, lambda _: pytest.fail("Слабое совпадение старой публикации требует сверки"))
    assert result["reason"] == "related_delivery_reconciliation_required"


def test_owner_closed_anchor_never_reopens_for_duplicate_audit(engine):
    seed_duplicate(engine)
    with Session(engine) as session:
        core.close_by_owner(session, 1, now=NOW)
        session.commit()
    result = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW).process(
        1, lambda _: pytest.fail("Отменённый владельцем материал не открывается"))
    assert result["reason"] == "owner_closed_backlog"
    assert result["lifecycle"]["audit_attempts"] == 0


@pytest.mark.parametrize("valid_hash", [True, False])
def test_hash_bound_saved_package_only_restores_transport_reconciliation(engine, valid_hash):
    seed_duplicate(engine)
    with Session(engine) as session:
        run, target = session.get(EditorialRun, 2), session.get(RawItem, 2)
        payload = package_payload()
        payload.update(raw_item_id=2, source_id=target.source_id, article_id=target.external_id,
            evidence=_document(target).model_dump(mode="json"), outcome=copy.deepcopy(run.outcome))
        for review in payload["reviews"]:
            review.update(source_id=target.source_id, article_id=target.external_id,
                context_sha256="0"*64, draft_sha256=draft_sha256(outcome(_document(target)).draft))
        bind_context(payload)
        run.outcome = {**run.outcome, "_package":payload, "_package_sha256":
            hashlib.sha256(canonical_json(payload).encode()).hexdigest() if valid_hash else "0"*64}
        run.delivery_status = "sent"
        session.get(Post, run.post_id).telegram_message_id = 123
        run.evidence = {"raw_item_id":2, "source_id":"official"}
        target.title, target.body, target.url = "Иная статья", "Это другой текущий снимок.", \
            "https://example.test/changed-completely/"
        session.commit()
        result = proof(session)
        assert (result.kind if result else None) == (
            "delivery_reconciliation" if valid_hash else None)
    if valid_hash:
        result = MaterialLifecycleRuntime(engine, "belgrade", NoModels(), NOW).process(
            1, lambda _: pytest.fail("Сохранённый пакет не разрешает новый выпуск"))
        assert result["reason"] == "related_delivery_reconciliation_required"
