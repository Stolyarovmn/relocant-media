import copy
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_editorial_package import NOW, bind_context, event_payload, parse
from test_editorial_pipeline import FakeProvider, _ranking
from test_policy_reassessment import state as _state

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument
from relocant_media.ai.package import evaluate_package
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.domain.audience import READER_VALUE_POLICY_VERSION
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import event_key
from relocant_media.services.editorial import _candidate, _document, _snapshot_member
from relocant_media.services.package_import import import_package
from relocant_media.services.policy_reassessment import (
    eligible_empty_policy_hold,
    eligible_reader_category_hold,
    persist_policy_reassessment,
)
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

state = _state


def test_second_sport_ranking_can_still_reject_without_forced_approval():
    provider = FakeProvider({"rank": _ranking("sport").model_copy(update={
        "should_process": False, "relevance": 0.1,
        "rationale": "Повтор результата без новых существенных сведений."})})
    document = EvidenceDocument(source_id="sport_news", url="https://example.test/sport",
        title="Нови Београд сыграл в водное поло",
        body="Beograd: vaterpolisti završili utakmicu. Rezultat je već objavljen.")
    outcome = EditorialPipeline(provider, city_profile=load_city_profile("belgrade")).run(
        city="belgrade", documents=(document,), now=NOW)
    assert provider.calls == ["rank", "rank"]
    assert outcome.status == "skipped"
    assert outcome.reason == "ranking_rejected"
    assert outcome.ranking.should_process is False
    assert outcome.draft is None


@pytest.mark.parametrize("category", ["culture", "sport", "city"])
def test_reader_categories_require_two_valid_independent_reviews(category):
    payload = event_payload()
    payload["outcome"]["ranking"]["category"] = category
    bind_context(payload)
    assert evaluate_package(parse(payload), now=NOW).status == "ready"
    payload["reviews"][1]["reviewer_id"] = payload["reviews"][0]["reviewer_id"]
    with pytest.raises(ValueError, match="two distinct"):
        evaluate_package(parse(payload), now=NOW)


@pytest.mark.parametrize("category", ["culture", "sport", "city"])
@pytest.mark.parametrize("guard", ["flag", "low_confidence", "high_risk", "hold", "stale"])
def test_reader_categories_preserve_review_and_risk_guards(category, guard):
    payload = event_payload()
    payload["outcome"]["ranking"]["category"] = category
    if guard == "flag":
        payload["reviews"][0]["flags"] = ["uncertain_date"]
    elif guard == "low_confidence":
        payload["outcome"]["verification"]["claims"][0]["confidence"] = 0.7
    elif guard == "high_risk":
        payload["outcome"]["ranking"]["category"] = "immigration"
    elif guard == "hold":
        payload["reviews"][1]["verdict"] = "hold"
    else:
        payload["reviews"][0]["reviewed_at"] = (NOW - timedelta(hours=25)).isoformat()
    bind_context(payload)
    assert evaluate_package(parse(payload), now=NOW).status == "human_review"


def _rejected_local_item(state, category="sport", local=True):
    session, _, run, _ = state
    item = session.get(RawItem, run.raw_item_id)
    source = session.get(Source, item.source_id)
    item.title = ("Vaterpolisti Novog Beograda u Ligi šampiona" if local
                  else "Vaterpolisti u Ligi šampiona")
    item.body = ("Novi Beograd je igrao utakmicu kod kuće." if local
                 else "Rezultat utakmice u Madridu.")
    doc = _document(item)
    run.evidence = {**doc.model_dump(mode="json"), "city": source.city,
                    "event_key": event_key(_candidate(item)),
                    "documents": [_snapshot_member(item, source)]}
    run.outcome = {"status": "skipped", "reason": "ranking_rejected",
                   "ranking": {"category": category},
                   "claims": None, "draft": None, "verification": None}
    session.commit()
    return session, item, run, (doc,)


@pytest.mark.parametrize(("category", "local", "expected"), [
    ("sport", True, True), ("culture", True, True), ("city", True, True),
    ("events", True, True), ("sport", False, False), ("politics", True, False),
])
def test_only_targeted_local_reader_rejections_reopen(state, category, local, expected):
    session, item, _, _ = _rejected_local_item(state, category, local)
    assert eligible_empty_policy_hold(session, item.id, now=NOW) is expected


def test_reader_ranking_reassessment_is_once_only_and_audited(state):
    session, item, run, documents = _rejected_local_item(state)
    previous = copy.deepcopy(run.outcome)
    outcome = EditorialOutcome.model_validate({**state[1].outcome.model_dump(),
        "status": "skipped", "reason": "Повторный независимый отбор"})
    assert eligible_empty_policy_hold(session, item.id, now=NOW)
    saved = persist_policy_reassessment(session, item.id, outcome, documents, now=NOW)
    assert saved.outcome["_previous_policy_hold"]["outcome"] == previous
    assessment = saved.outcome["_autonomous_assessment"]
    assert assessment["reader_value_policy"] == READER_VALUE_POLICY_VERSION
    assert not eligible_empty_policy_hold(session, item.id, now=NOW)
    with pytest.raises(ValueError, match="not eligible"):
        persist_policy_reassessment(session, item.id, outcome, documents, now=NOW)
    assert session.scalar(select(func.count()).select_from(Post)) == 0
    assert run.created_at == NOW


@pytest.mark.parametrize("guard", ["receipt", "sending", "evidence", "snapshot", "old", "assessed"])
def test_reader_rejection_reassessment_preserves_existing_guards(state, guard):
    session, item, run, _ = _rejected_local_item(state)
    now = NOW
    if guard == "receipt":
        run.staging_message_id = 17
    elif guard == "sending":
        run.delivery_status = "sending"
    elif guard == "evidence":
        item.body += " Изменение первоисточника."
    elif guard == "snapshot":
        evidence = copy.deepcopy(run.evidence)
        evidence["documents"][0]["auto_publish"] = True
        run.evidence = evidence
    elif guard == "old":
        now += timedelta(days=8)
    else:
        run.outcome = {**run.outcome, "_autonomous_assessment": {}}
    session.commit()
    assert not eligible_empty_policy_hold(session, item.id, now=now)


def _legacy_category_hold(state, category="culture"):
    session, package, run, _ = state
    payload = package.model_dump(mode="json")
    payload["outcome"]["ranking"]["category"] = category
    bind_context(payload)
    package = parse(payload)
    run = import_package(session, package, now=NOW, replace_empty_hold=True)
    # Восстанавливаем вид старого пакета, отложенного до расширения категорий.
    outcome = copy.deepcopy(run.outcome)
    outcome.update(status="human_review", reason="package_requires_review")
    outcome.pop("_autonomous_assessment", None)
    outcome.pop("_previous_policy_hold", None)
    run.outcome = outcome
    session.commit()
    return session, package, run


@pytest.mark.parametrize("category", ["culture", "sport", "city"])
def test_legacy_reader_category_hold_requires_explicit_reviewed_import(state, category):
    session, package, run = _legacy_category_hold(state, category)
    previous = copy.deepcopy(run.outcome)
    assert eligible_reader_category_hold(session, 1, now=NOW)
    saved = import_package(session, package, now=NOW, replace_map_hold=True)
    assert saved.outcome["status"] == "human_review"
    assert saved.outcome["reason"] == "source_auto_publish_disabled"
    assert saved.outcome["_previous_policy_hold"]["outcome"] == previous
    assert saved.outcome["_autonomous_assessment"] == {
        "reviewed_package": True, "reader_value_policy": READER_VALUE_POLICY_VERSION}
    assert saved.created_at == NOW
    assert not eligible_reader_category_hold(session, 1, now=NOW)
    audit = copy.deepcopy(saved.outcome)
    post_id = saved.post_id
    saved.staging_message_id = 99
    session.commit()
    again = import_package(session, package, now=NOW, replace_map_hold=True)
    assert again.outcome == audit
    assert again.post_id == post_id
    assert again.staging_message_id == 99
    assert session.scalar(select(func.count()).select_from(EditorialRun)) == 1


@pytest.mark.parametrize("guard", [
    "receipt", "sending", "published", "telegram", "draft", "evidence", "snapshot",
    "fingerprint", "event_key", "old", "assessed", "rich_delivery", "media_delivery",
])
def test_legacy_category_hold_requires_exact_unpublished_snapshot(state, guard):
    session, package, run = _legacy_category_hold(state)
    post = session.get(Post, run.post_id)
    now = NOW
    if guard == "receipt":
        run.staging_message_id = 17
    elif guard == "sending":
        run.delivery_status = "sending"
    elif guard == "published":
        post.published_at = NOW
    elif guard == "telegram":
        post.telegram_message_id = 108
    elif guard == "draft":
        post.body += " Ручная правка."
    elif guard == "evidence":
        session.get(RawItem, 1).body += " Новые сведения."
    elif guard == "snapshot":
        session.get(Source, package.source_id).auto_publish = True
    elif guard == "fingerprint":
        run.outcome = {**run.outcome, "_package_sha256": "0" * 64}
    elif guard == "event_key":
        evidence = copy.deepcopy(run.evidence)
        evidence["documents"][0]["body"] += " Изменённый снимок."
        run.evidence = evidence
    elif guard == "old":
        now += timedelta(days=8)
    elif guard == "assessed":
        run.outcome = {**run.outcome, "_autonomous_assessment": {}}
    else:
        run.outcome = {**run.outcome, "_" + guard: {}}
    session.commit()
    assert not eligible_reader_category_hold(session, 1, now=now)


def test_source_high_risk_is_not_removed_by_reader_category_import(state):
    session, package, run, _ = state
    source = session.get(Source, package.source_id)
    item = session.get(RawItem, 1)
    source.auto_publish = True
    source.high_risk = True
    snapshot = _snapshot_member(item, source)
    run.evidence = {**run.evidence, "documents": [snapshot]}
    session.commit()
    payload = package.model_dump(mode="json")
    payload["outcome"]["ranking"]["category"] = "city"
    payload["event"]["documents"] = [snapshot]
    bind_context(payload)
    saved = import_package(session, parse(payload), now=NOW, replace_empty_hold=True)
    assert saved.outcome["status"] == "human_review"
    assert saved.outcome["reason"] == "source_policy_requires_review"
