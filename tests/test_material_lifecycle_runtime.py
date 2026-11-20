"""Runtime не обходит штатный пакет и сохраняет границы перед моделью."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument, RankingResult
from relocant_media.ai.public_cache import PublicPreparationCache
from relocant_media.domain.policy import SOURCE_REPLACED_REASON
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import material_lifecycle as core
from relocant_media.services.material_lifecycle_runtime import MaterialLifecycleRuntime
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
BODY = "Air Serbia upozorava na kašnjenja zbog nepogode u Beogradu."


class Provider:
    def __init__(self, engine, *, decision="retry_processing", error=None, audit_changes=None):
        self.engine, self.decision, self.error = engine, decision, error
        self.audit_changes = audit_changes or {}
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["task"] == "audit_rejection":
            # Отдельная сессия видит сохранённую попытку до внешнего вызова.
            with Session(self.engine) as session:
                record = session.scalar(select(SchedulerRun))
                assert record.details["state"] == "processing"
                assert record.details["audit_attempts"] >= 1
            if self.error:
                raise self.error
            return json.dumps(dict(raw_item_id=1, decision=self.decision,
                basis=("unsupported_rejection" if self.decision == "retry_processing"
                       else "supported_rejection"),
                rubric_id="city_changes",
                criterion=("unsupported_rejection" if self.decision == "retry_processing"
                           else "insufficient_reader_interest"),
                missing_required_details=[],
                reason="Проверка исходных фактов и обоснованности прежнего отказа.",
                evidence_refs=[dict(source_id="official", url="https://example.test/notice/",
                                    quote=BODY)]) | self.audit_changes)
        return json.dumps(rank())


def rank():
    return dict(should_process=False, relevance=0.2, category="transport", confidence=0.8,
                rationale="Без конкретных номеров рейсов нельзя отбирать предупреждение.")


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Source(id="official", city="belgrade", name="Официальный источник",
            url="https://example.test", source_type="official_news", trust=1,
            auto_publish=True, enabled=True, high_risk=False))
        session.flush()
        session.add(RawItem(id=1, source_id="official", external_id="1",
            url="https://example.test/notice/", title="Предупреждение авиакомпании", body=BODY,
            published_at=NOW-timedelta(hours=1), published_at_precision="instant",
            ingested_at=NOW))
        session.commit()
    yield engine
    engine.dispose()


def rejected(engine, **changes):
    with Session(engine) as session:
        raw = session.get(RawItem, 1)
        document = EvidenceDocument(source_id=raw.source_id, url=raw.url, title=raw.title,
            body=raw.body, published_at=raw.published_at,
            published_at_precision=raw.published_at_precision).model_dump(mode="json")
        session.add(EditorialRun(raw_item_id=1,
            outcome=dict(status="skipped", reason="not_selected", ranking=rank()) | changes,
            evidence={**document, "city":"belgrade",
                      "documents":[dict(document, raw_item_id=1)]},
            delivery_status="pending", created_at=NOW))
        session.commit()


def ledger(engine):
    with Session(engine) as session:
        return session.scalar(select(SchedulerRun)).details


def test_refuted_rejection_archives_then_delegates_full_pipeline(engine):
    rejected(engine)
    provider = Provider(engine)
    runtime = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW)
    called = []

    def delegate(raw_id):
        called.append(raw_id)
        with Session(engine) as session:
            assert session.get(EditorialRun, raw_id) is None
            record = session.scalar(select(SchedulerRun))
            assert record.details["state"] == "processing"
            assert record.details["attempts"] == 1
            assert record.details["attempt_records"][0]["outcome"]["ranking"] == rank()
            assert record.details["rejection_review"]["decision"] == "retry_processing"
        # Настоящий delegate должен заново выполнить claims/facts/draft и оба review.
        rejected(engine)
        return dict(raw_item_id=raw_id, status="skipped", reason="new_full_pipeline_rejection")

    assert runtime.reconcile().rejection_audit_ids == (1,)
    result = runtime.process(1, delegate)
    assert called == [1] and len(provider.calls) == 1
    assert result["lifecycle"]["state"] == "blocked"
    assert ledger(engine)["reason"] == "repeated_editorial_rejection"


def test_confirmed_rejection_never_calls_delegate(engine):
    rejected(engine)
    provider = Provider(engine, decision="confirm_rejection")
    runtime = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW)
    result = runtime.process(1, lambda _: pytest.fail("Отказ не разрешает публикацию"))
    assert result["status"] == "blocked"
    assert ledger(engine)["rejection_review"]["decision"] == "confirm_rejection"
    with Session(engine) as session:
        assert session.get(EditorialRun, 1) is not None


def test_bounded_drain_can_stop_between_audit_and_full_retry(engine):
    rejected(engine)
    provider = Provider(engine)
    runtime = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW)
    result = runtime.process(1, lambda _: pytest.fail("Повтор — отдельная попытка"),
                             retry_refuted=False)
    assert result["lifecycle"]["next_action"] == "retry_prepare"
    state = ledger(engine)
    assert state["audit_attempts"] == 1 and state["attempts"] == 0
    assert len(provider.calls) == 1
    with Session(engine) as session:
        assert session.get(EditorialRun, 1) is not None


def unsupported_policy_provider(engine):
    return Provider(engine, decision="confirm_rejection", audit_changes={
        "rubric_id": "city_life", "criterion": "missing_required_details",
        "missing_required_details": ["price"],
    })


def test_inapplicable_confirm_uses_existing_full_retry_without_new_budget_or_ready(engine):
    rejected(engine, ranking=rank() | {"category": "sport"})
    provider = unsupported_policy_provider(engine)
    runtime = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW)
    runtime.reconcile()
    original = ledger(engine)
    result = runtime.process(1, lambda _: pytest.fail("Повтор требует отдельного запуска"),
                             retry_refuted=False)
    state = ledger(engine)
    assert result["lifecycle"]["next_action"] == "retry_prepare"
    assert state["attempts"] == 0 and state["audit_attempts"] == 1
    assert state["rejection_justified"] is False
    review = state["rejection_review"]
    assert review["decision"] == "retry_processing" and review["basis"] == "unsupported_rejection"
    assert review["policy_issues"] == ["detail_not_required_by_rubric"]
    assert review["original_assessment"]["decision"] == "confirm_rejection"
    assert review["policy_version"] == "rejection-review-v2"
    assert all(state[key] == original[key] for key in
               ("first_seen_at", "deadline_at", "fingerprints", "fallback_attempts"))
    called = []

    def full_pipeline(raw_id):
        called.append(raw_id)
        with Session(engine) as session:
            assert session.get(EditorialRun, raw_id) is None
            assert session.scalar(select(SchedulerRun)).details["attempts"] == 1
        # Модельный аудит не заменяет повторные claims/facts/draft и оба ревью.
        rejected(engine, ranking=rank() | {"category": "sport"})
        return {"status": "skipped", "raw_item_id": raw_id}

    final = runtime.process(1, full_pipeline)
    assert called == [1] and len(provider.calls) == 1
    assert final["lifecycle"]["state"] == "blocked"
    state = ledger(engine)
    assert state["reason"] == "repeated_editorial_rejection" and state["attempts"] == 1
    assert all(state[key] == original[key] for key in
               ("first_seen_at", "deadline_at", "fingerprints", "fallback_attempts"))
    with Session(engine) as session:
        assert session.scalar(select(Post)) is None
        assert session.get(EditorialRun, 1).outcome["ranking"]["category"] == "sport"


def test_legacy_v1_saved_retry_still_invalidates_rank_cache_without_migration(engine):
    runtime = MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW)
    runtime.reconcile()
    legacy = {"policy_version": "rejection-review-v1", "decision": "retry_processing",
              "basis": "unsupported_rejection", "context_sha256": "a" * 64,
              "reason": "Сохранённое прежнее решение требует полного повтора."}
    with Session(engine) as session:
        record = session.scalar(select(SchedulerRun))
        record.details = {**record.details, "rejection_review": legacy}
        session.commit()
    runtime.provider_for(1).generate_json(task="rank", system_instruction="Политика.",
        prompt="Исходный источник.", response_schema=RankingResult.model_json_schema())
    assert "rejection_review_context_sha256=" + "a" * 64 in (
        runtime.provider.calls[0]["system_instruction"])
    assert ledger(engine)["rejection_review"] == legacy


@pytest.mark.parametrize("protection", ["owner_closed", "sent", "sending", "uncertain", "staging"])
def test_inapplicable_confirm_cannot_reopen_closed_or_delivered_material(engine, protection):
    rejected(engine, ranking=rank() | {"category": "sport"})
    provider = unsupported_policy_provider(engine)
    runtime = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW)
    runtime.reconcile()
    with Session(engine) as session:
        if protection == "owner_closed":
            core.close_by_owner(session, 1, now=NOW)
        else:
            post = Post(city="belgrade", category="sport", status="ready", title="Источник",
                        body=BODY, confidence=1,
                        telegram_message_id=123 if protection == "sent" else None)
            session.add(post)
            session.flush()
            run = session.get(EditorialRun, 1)
            run.post_id = post.id
            run.delivery_status = "pending" if protection == "staging" else protection
            if protection == "staging":
                run.staging_message_id = 456
        session.commit()
    result = runtime.process(1, lambda _: pytest.fail("Защищённый материал не переоткрывается"))
    assert result["status"] in {"blocked", "published"} and provider.calls == []
    assert ledger(engine)["attempts"] == 0 and ledger(engine)["audit_attempts"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, 1)
        assert run is not None
        if protection == "owner_closed":
            assert ledger(engine)["reason"] == "owner_closed_backlog"
        elif protection == "staging":
            assert run.staging_message_id == 456
        else:
            assert run.delivery_status == protection
            if protection == "sent":
                assert session.get(Post, run.post_id).telegram_message_id == 123


def test_refuted_rejection_cannot_remove_reviewed_or_private_package(engine):
    rejected(engine, _package={"must_preserve":"Проверенный пакет"})
    runtime = MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW)
    with pytest.raises(ValueError, match="архивировать"):
        runtime.process(1, lambda _: pytest.fail("Нельзя обходить защиту архивирования"))
    with Session(engine) as session:
        run = session.get(EditorialRun, 1)
        assert run.outcome["_package"] == {"must_preserve":"Проверенный пакет"}
    assert ledger(engine).get("attempt_records", []) == []


def test_changed_stored_evidence_uses_bounded_error_without_model(engine):
    rejected(engine)
    with Session(engine) as session:
        run = session.get(EditorialRun, 1)
        snapshots = run.evidence["documents"]
        run.evidence = {**run.evidence,
                        "documents":[{**snapshots[0], "body":"Подменённое доказательство"}]}
        session.commit()
    provider = Provider(engine)
    result = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW).process(
        1, lambda _: pytest.fail("Подмена доказательства не разрешает повтор"))
    assert provider.calls == []
    assert result["lifecycle"]["next_action"] == "audit_rejection"
    assert result["lifecycle"]["audit_attempts"] == 1


def test_audit_failure_is_bounded_retry_without_confirming_rejection(engine):
    rejected(engine)
    provider = Provider(engine, error=TimeoutError("секретная причина"))
    runtime = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW)
    first = runtime.process(1, lambda _: pytest.fail("Ошибка не даёт разрешение"))
    assert first["lifecycle"]["next_action"] == "audit_rejection"
    assert first["lifecycle"]["audit_attempts"] == 1
    assert "rejection_review" not in ledger(engine)
    assert "секретная" not in json.dumps(ledger(engine), ensure_ascii=False)
    runtime.process(1, lambda _: pytest.fail("Повтор до срока запрещён"))
    assert len(provider.calls) == 1
    runtime.now = NOW + timedelta(minutes=6)
    final = runtime.process(1, lambda _: pytest.fail("Ошибка не даёт разрешение"))
    assert final["status"] == "blocked" and len(provider.calls) == 2


@pytest.mark.parametrize("change", ["expired", "risk", "source_changed"])
def test_deterministic_guards_precede_model(engine, change):
    rejected(engine)
    if change == "source_changed":
        MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW).reconcile()
    with Session(engine) as session:
        if change == "expired":
            session.get(RawItem, 1).published_at = NOW-timedelta(days=15)
        elif change == "risk":
            session.get(Source, "official").high_risk = True
        else:
            session.get(RawItem, 1).body = "Изменённое доказательство после создания ledger."
        session.commit()
    provider = Provider(engine)
    result = MaterialLifecycleRuntime(engine, "belgrade", provider, NOW).process(
        1, lambda _: pytest.fail("Детерминированная блокировка обязательна"))
    assert result["status"] == "blocked" and provider.calls == []


def test_initial_items_still_use_selector_and_failed_attempt_has_due_retry(engine):
    runtime = MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW)
    sweep = runtime.reconcile()
    assert sweep.due_ids == () and 1 not in sweep.excluded_ids
    original_error = TimeoutError("сбой")
    with pytest.raises(TimeoutError) as raised:
        runtime.process(1, lambda _: (_ for _ in ()).throw(original_error))
    assert raised.value is original_error
    assert ledger(engine)["attempts"] == 1
    assert ledger(engine)["next_action"] == "retry_prepare"
    assert runtime.reconcile().excluded_ids == frozenset({1})
    runtime.now = NOW+timedelta(minutes=6)
    assert runtime.reconcile().due_ids == (1,)


def test_refuted_rank_uses_distinct_cache_key_but_other_tasks_are_unchanged(engine, tmp_path):
    rejected(engine)
    underlying = Provider(engine)
    cached = PublicPreparationCache(underlying, tmp_path / "cache", model_id="fixture",
                                    public_news_only=True, now=lambda: NOW)
    runtime = MaterialLifecycleRuntime(engine, "belgrade", cached, NOW)
    args = dict(task="rank", system_instruction="Политика отбора.", prompt="Исходные документы.",
                response_schema=RankingResult.model_json_schema())
    cached.generate_json(**args)
    cached.generate_json(**args)
    assert len(underlying.calls) == 1
    runtime.process(1, lambda raw_id: {"raw_item_id":raw_id, "status":"test_only"})
    wrapper = runtime.provider_for(1)
    wrapper.generate_json(**args)
    wrapper.generate_json(**args)
    assert len(underlying.calls) == 3  # Первый rank, аудит, новый rank.
    assert "rejection_review_context_sha256" in underlying.calls[-1]["system_instruction"]
    assert ledger(engine)["rejection_review"]["decision"] == "retry_processing"


def test_uncertain_delivery_is_blocked_without_mutating_receipt(engine):
    rejected(engine)
    with Session(engine) as session:
        post = Post(city="belgrade", category="transport", status="ready",
                    title="Предупреждение", body=BODY, confidence=1)
        session.add(post)
        session.flush()
        run = session.get(EditorialRun, 1)
        run.post_id, run.delivery_status = post.id, "uncertain"
        session.commit()
    runtime = MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW)
    result = runtime.delivery_failed(1, TimeoutError("результат не известен"))
    assert result["status"] == "blocked"
    with Session(engine) as session:
        assert session.get(EditorialRun, 1).delivery_status == "uncertain"


def test_existing_post_without_supported_repair_blocks_without_fictitious_attempt(engine):
    rejected(engine, status="human_review", reason="facts_review_failed")
    with Session(engine) as session:
        post = Post(city="belgrade", category="transport", status="human_review",
                    title="Предупреждение", body=BODY, confidence=0.5)
        session.add(post)
        session.flush()
        session.get(EditorialRun, 1).post_id = post.id
        session.commit()
    runtime = MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW)
    runtime.reconcile()
    runtime.now = NOW + timedelta(minutes=6)
    result = runtime.process(1, lambda _: pytest.fail("Подготовка вернёт старый run без ремонта"))
    assert result["status"] == "blocked"
    assert result["reason"] == "unsupported_repair"
    assert ledger(engine)["attempts"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, 1)
        post = session.get(Post, run.post_id)
        assert (post.title, post.body) == ("Предупреждение", BODY)


@pytest.mark.parametrize("fails", [False, True])
def test_source_replacement_inherits_budget_and_error_targets_actual_material(engine, fails):
    runtime = MaterialLifecycleRuntime(engine, "belgrade", Provider(engine), NOW)
    runtime.reconcile()
    original_deadline = ledger(engine)["deadline_at"]
    original_error = TimeoutError("Сбой проверки нового первоисточника")

    def delegate(_):
        with Session(engine) as session:
            session.add(RawItem(id=2, source_id="official", external_id="2",
                url="https://example.test/primary/", title="Исходное предупреждение", body=BODY,
                published_at=NOW-timedelta(hours=1), published_at_precision="instant",
                ingested_at=NOW))
            session.flush()
            session.add(EditorialRun(raw_item_id=1, delivery_status="pending", created_at=NOW,
                evidence={}, outcome={"status":"skipped", "reason":SOURCE_REPLACED_REASON,
                                      "replacement_raw_item_id":2}))
            if not fails:
                session.add(EditorialRun(raw_item_id=2, delivery_status="pending", created_at=NOW,
                    evidence={}, outcome={"status":"skipped", "reason":"not_selected",
                                          "ranking":rank()}))
            session.commit()
        if fails:
            raise original_error
        return {"raw_item_id":2, "status":"skipped", "replaced_unavailable_raw_item_id":1}

    if fails:
        with pytest.raises(TimeoutError) as raised:
            runtime.process(1, delegate)
        assert raised.value is original_error
    else:
        runtime.process(1, delegate)
    with Session(engine) as session:
        state = core.lifecycle_state(session, 2, now=NOW)
        assert state["attempts"] == 1 and state["deadline_at"] == original_deadline
        assert state["replacement_raw_item_id"] == 2
        assert state["next_action"] == ("retry_prepare" if fails else "audit_rejection")
        if fails:
            assert state["last_error_type"] == "TimeoutError"
        original = session.get(EditorialRun, 1)
        assert original.outcome["reason"] == SOURCE_REPLACED_REASON
        assert original.delivery_status == "pending"
