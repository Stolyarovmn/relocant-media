"""Неподтверждённый текст не проходит импорт/доставку или решение владельца."""

import copy
import hashlib
import json
from datetime import UTC, datetime

import pytest
from facts_check_helpers import facts_check_for
from sqlalchemy.orm import Session
from test_channel_delivery import FakePublisher, send
from test_channel_delivery import reviewed as channel_reviewed
from test_gpt_fallback_service import NOW, package_for_request
from test_gpt_fallback_service import fallback_session as service_session
from test_owner_review import (
    Publisher,
    _live,
    _owner_review_row,
    _package_hash,
    _store_reviewed_politics,
    _stored_package,
)
from test_owner_review import engine as owner_engine

from relocant_media.ai.package import (
    EditorialPackage,
    canonical_json,
    context_sha256,
    evaluate_package,
)
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.services import gpt_fallback
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.editorial import DeliveryBlockedError
from relocant_media.services.material_lifecycle_runtime import MaterialLifecycleRuntime
from relocant_media.services.owner_review import _validated_pending_package, deliver_owner_approved
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

reviewed = channel_reviewed
fallback_session = service_session
engine = owner_engine


def _legacy_package(session, run):
    payload = copy.deepcopy(run.outcome["_package"])
    for review in payload["reviews"]:
        review.pop("facts_check", None)
    package = EditorialPackage.model_validate_json(json.dumps(payload))
    assert package.model_dump(mode="json") == payload
    fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    run.outcome = {**run.outcome, "_package": payload, "_package_sha256": fingerprint}
    session.commit()
    return package


@pytest.mark.asyncio
async def test_old_pending_perfect_approvals_need_coverage_before_channel_send(reviewed):
    session, run = reviewed
    _legacy_package(session, run)
    before = copy.deepcopy(run.outcome)
    publisher = FakePublisher()
    with pytest.raises(DeliveryBlockedError, match="editorial policy"):
        await send(session, run, publisher)
    session.refresh(run)
    assert publisher.calls == [] and run.delivery_status == "pending"
    assert run.outcome == before
    runtime = MaterialLifecycleRuntime(session.get_bind(), "belgrade", None, datetime.now(UTC))
    result = runtime.process(run.raw_item_id, lambda _: pytest.fail("Старый READY не оживает"))
    assert result["lifecycle"]["state"] == "blocked"
    assert result["lifecycle"]["reason"] == "draft_coverage_required"
    assert result["lifecycle"]["attempts"] == result["lifecycle"]["audit_attempts"] == 0


@pytest.mark.asyncio
async def test_legacy_sent_receipt_and_exact_import_remain_idempotent(reviewed):
    session, run = reviewed
    package = _legacy_package(session, run)
    post = session.get(Post, run.post_id)
    run.delivery_status = "sent"
    post.telegram_message_id = 42
    post.published_at = datetime.now(UTC)
    session.commit()
    before = copy.deepcopy(run.outcome)
    publisher = FakePublisher()
    assert (await send(session, run, publisher)).message_id == 42
    replay = import_package(session, package)
    assert replay.delivery_status == "sent" and replay.outcome == before
    assert session.get(Post, replay.post_id).telegram_message_id == 42
    assert publisher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sending", "uncertain"])
async def test_missing_coverage_does_not_retry_unknown_delivery(reviewed, status):
    session, run = reviewed
    _legacy_package(session, run)
    run.delivery_status = status
    session.commit()
    publisher = FakePublisher()
    with pytest.raises(DeliveryBlockedError, match="manual reconciliation"):
        await send(session, run, publisher)
    session.refresh(run)
    assert run.delivery_status == status and publisher.calls == []


@pytest.mark.parametrize("missing", [True, False])
def test_external_gpt_cannot_import_missing_or_unsupported_coverage(fallback_session, missing):
    session, _, _ = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    payload = package_for_request(request).model_dump(mode="json")
    facts = next(review for review in payload["reviews"] if review["role"] == "facts")
    if missing:
        facts.pop("facts_check")
    else:
        facts["facts_check"]["segments"][0]["assessment"] = "unsupported"
    package = EditorialPackage.model_validate_json(json.dumps(payload))
    with pytest.raises(gpt_fallback.FallbackValidationError, match="редакционные проверки"):
        gpt_fallback.validate_fallback_package(session, request, package, now=NOW)
    assert session.get(EditorialRun, package.raw_item_id) is None


@pytest.mark.asyncio
async def test_owner_approval_cannot_cancel_unsupported_factual_coverage(engine):
    raw_id = _store_reviewed_politics(engine)
    publisher = Publisher()
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        payload = _stored_package(run).model_dump(mode="json")
        facts = next(review for review in payload["reviews"] if review["role"] == "facts")
        facts["facts_check"]["segments"][0]["assessment"] = "unsupported"
        package = EditorialPackage.model_validate_json(json.dumps(payload))
        run.outcome = {**run.outcome, "_package": payload,
                       "_package_sha256": _package_hash(package)}
        review = _owner_review_row(session, raw_id)
        review.status = "approved"
        session.commit()
        with pytest.raises(DeliveryBlockedError, match="blockers beyond owner approval"):
            await deliver_owner_approved(session, review, publisher, live_documents=_live(package),
                target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
        assert publisher.calls == []
        assert session.get(EditorialRun, raw_id).delivery_status == "pending"


def test_two_existing_text_repairs_end_in_explained_lifecycle_block(reviewed):
    session, run = reviewed
    package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    initial = package.model_copy(update={"reviews": tuple(review.model_copy(update={
        "verdict": "hold"}) for review in package.reviews)})

    class UnsupportedProvider:
        def __init__(self):
            self.calls = []

        def generate_json(self, *, task, prompt, **kwargs):
            self.calls.append(task)
            context = json.loads(prompt)["package"]
            if task == "repair_draft":
                draft = context["outcome"]["draft"]
                draft["title"] = "[тестовый запуск] Уточнение " + str(len(self.calls))
                return json.dumps(draft)
            response = {"verdict": "approve", "score": 100, "flags": [],
                        "reason": "Синтетическая независимая проверка."}
            if task == "independent_facts":
                response["facts_check"] = facts_check_for(context)
                response["facts_check"]["segments"][0]["assessment"] = "unsupported"
            else:
                response["rubric"] = {"local_relevance": 4, "practical_value": 4,
                                     "timeliness": 4, "clarity": 4}
            return json.dumps(response)

    provider = UnsupportedProvider()
    final = review_with_repairs(initial, provider, lambda: _live(initial), max_repairs=2)
    assert provider.calls.count("repair_draft") == 2
    assert provider.calls.count("independent_facts") == 3
    assert provider.calls.count("independent_utility") == 3
    evaluated = evaluate_package(final)
    assert evaluated.reason == "draft_coverage_unsupported"
    post = session.get(Post, run.post_id)
    post.title, post.body = final.outcome.draft.title, final.outcome.draft.body
    post.status = "human_review"
    run.outcome = {**evaluated.model_dump(mode="json"), "_package": final.model_dump(mode="json"),
                   "_package_sha256": _package_hash(final)}
    session.commit()
    runtime = MaterialLifecycleRuntime(session.get_bind(), "belgrade", provider, datetime.now(UTC))
    before = list(provider.calls)
    result = runtime.process(run.raw_item_id,
        lambda _: pytest.fail("Закрытый пакет не обрабатывается"))
    state = result["lifecycle"]
    assert state["state"] == "blocked" and state["reason"] == "draft_coverage_unsupported"
    assert state["attempts"] == state["audit_attempts"] == state["fallback_attempts"] == 0
    assert state["next_action"] is None and state["next_attempt_at"] is None
    assert provider.calls == before


def test_independent_sensitivity_is_separate_gate_without_text_repair(reviewed):
    session, run = reviewed
    payload = copy.deepcopy(run.outcome["_package"])
    facts = next(review for review in payload["reviews"] if review["role"] == "facts")
    facts["facts_check"]["publication_scope"] = "sensitive_substance"
    package = EditorialPackage.model_validate_json(json.dumps(payload))
    run.outcome = {**run.outcome, "_package": payload, "_package_sha256": _package_hash(package)}
    session.commit()
    runtime = MaterialLifecycleRuntime(session.get_bind(), "belgrade", None, datetime.now(UTC))
    result = runtime.process(run.raw_item_id,
        lambda _: pytest.fail("Риск не чинится переписыванием"))
    assert result["lifecycle"]["reason"] == "separate_publication_approval_required"
    assert result["lifecycle"]["state"] == "blocked"
    assert result["lifecycle"]["attempts"] == result["lifecycle"]["audit_attempts"] == 0


@pytest.mark.parametrize("scope", ["sensitive_substance", "uncertain"])
@pytest.mark.parametrize("unsupported", [False, True])
def test_risk_hold_does_not_trigger_textual_repairs(reviewed, scope, unsupported):
    session, run = reviewed
    package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    initial = package.model_copy(update={"reviews": tuple(review.model_copy(update={
        "verdict": "hold"}) for review in package.reviews)})
    calls = []

    class RiskProvider:
        def generate_json(self, *, task, prompt, **kwargs):
            calls.append(task)
            if task == "repair_draft":
                pytest.fail("Классификация риска не исправляется переписыванием текста")
            response = {"verdict": "approve", "score": 100, "flags": [],
                        "reason": "Синтетическая независимая проверка."}
            if task == "independent_facts":
                check = facts_check_for(json.loads(prompt)["package"])
                check["publication_scope"] = scope
                if unsupported:
                    check["segments"][0]["assessment"] = "unsupported"
                response.update(verdict="hold", flags=["Требуется отдельный допуск риска"],
                                facts_check=check)
            else:
                response["rubric"] = {"local_relevance": 4, "practical_value": 4,
                                     "timeliness": 4, "clarity": 4}
            return json.dumps(response)

    result = review_with_repairs(initial, RiskProvider(), lambda: _live(initial))
    assert calls == ["independent_facts", "independent_utility"]
    assert result.outcome.draft == initial.outcome.draft
    assert evaluate_package(result).status == "human_review"


@pytest.mark.parametrize("scope,ownership,category,scope_ids,block", [
    ("sensitive_substance", "media", "transport", ["c1"], "official primary"),
    ("sensitive_substance", "official", "transport", ["c1"], None),
    ("uncertain", "official", "transport", [], "blockers beyond owner approval"),
    ("confirmed_transport_notice", "official", "transport", [], "blockers beyond owner approval"),
    ("confirmed_transport_notice", "official", "culture", ["c1"], "blockers beyond owner approval"),
    ("ordinary_local", "media", "culture", [], None),
])
def test_independent_risk_preserves_owner_provenance_and_classification_guards(
    engine, scope, ownership, category, scope_ids, block,
):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        raw = session.get(RawItem, raw_id)
        session.get(Source, raw.source_id).ownership = ownership
        payload = _stored_package(run).model_dump(mode="json")
        payload["outcome"]["ranking"]["category"] = category
        payload["event"]["documents"][0]["ownership"] = ownership
        facts = next(review for review in payload["reviews"] if review["role"] == "facts")
        facts["facts_check"].update(publication_scope=scope, scope_claim_ids=scope_ids)
        package = EditorialPackage.model_validate_json(json.dumps(payload))
        for review in payload["reviews"]:
            review["context_sha256"] = context_sha256(package)
        package = EditorialPackage.model_validate_json(json.dumps(payload))
        run.outcome = {**run.outcome, "_package": payload,
                       "_package_sha256": _package_hash(package)}
        review = _owner_review_row(session, raw_id)
        review.status = "approved"
        session.commit()
        if block:
            with pytest.raises(DeliveryBlockedError, match=block):
                _validated_pending_package(session, raw_id)
        else:
            assert _validated_pending_package(session, raw_id)[0].raw_item_id == raw_id
