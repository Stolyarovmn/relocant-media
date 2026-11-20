"""Поддельные полные пакеты проверяют покрытие текста, а не оценки approve."""

import copy

import pytest
from facts_check_helpers import bind_facts_check
from test_editorial_package import NOW, bind_context, event_payload, parse

from relocant_media.ai.package import draft_sha256, evaluate_package


def synthetic_package(*, transport_notice=False, category="transport"):
    payload = event_payload()
    quote = "Объединение проведёт собрание на городской площади."
    statements = [("c1", quote)]
    if transport_notice:
        statements.append(("c2", "Город сообщил: проезд по улице Садовой будет закрыт."))
    body = " ".join(text for _, text in statements)
    payload["evidence"].update(title="Городское объявление", body=body)
    payload["event"]["documents"][0].update(title="Городское объявление", body=body)
    payload["outcome"]["ranking"]["category"] = category
    payload["outcome"]["claims"]["claims"] = [
        {"claim_id": claim_id, "text": text, "material": True, "citations": [{
            "source_id": payload["source_id"], "url": payload["evidence"]["url"],
            "quote": text}]} for claim_id, text in statements]
    payload["outcome"]["verification"]["claims"] = [
        {"claim_id": claim_id, "supported": True, "confidence": 0.99,
         "reason": "Синтетический факт прямо указан в источнике."}
        for claim_id, _ in statements]
    payload["outcome"]["draft"] = {
        "title": "На площади состоится собрание", "body": body,
        "claim_ids": [claim_id for claim_id, _ in statements]}
    if transport_notice:
        payload["outcome"]["draft"]["title"] = "Проезд по Садовой временно закроют"
    seal(payload)
    return payload


def seal(payload):
    bind_facts_check(payload)
    draft = parse(payload).outcome.draft
    for review in payload["reviews"]:
        review.update(draft_sha256=draft_sha256(draft), score=100)
    bind_context(payload)


@pytest.mark.parametrize("field", ["title", "body"])
def test_uncovered_new_consequence_fails_even_with_two_perfect_approvals(field):
    payload = synthetic_package()
    payload["outcome"]["draft"][field] += " Проезд будет ограничен."
    draft = parse(payload).outcome.draft
    for review in payload["reviews"]:
        review["draft_sha256"] = draft_sha256(draft)
    bind_context(payload)
    outcome = evaluate_package(parse(payload), now=NOW)
    assert outcome.status == "human_review"
    assert outcome.reason == "draft_coverage_incomplete"


@pytest.mark.parametrize("assessment", ["unsupported", "uncertain"])
def test_explicit_unconfirmed_consequence_cannot_be_approved(assessment):
    payload = synthetic_package()
    payload["outcome"]["draft"]["title"] = "Собрание ограничит движение в городе"
    seal(payload)
    payload["reviews"][0]["facts_check"]["segments"][0].update(
        assessment=assessment, claim_ids=[], citations=[])
    outcome = evaluate_package(parse(payload), now=NOW)
    assert outcome.status == "human_review"
    assert outcome.reason == "draft_coverage_unsupported"


@pytest.mark.parametrize("mutation", ["unknown_claim", "unverified_claim", "foreign_quote"])
def test_coverage_requires_verified_claim_and_its_own_exact_citation(mutation):
    payload = synthetic_package(transport_notice=True)
    segment = payload["reviews"][0]["facts_check"]["segments"][0]
    if mutation == "unknown_claim":
        segment["claim_ids"] = ["invented"]
    elif mutation == "unverified_claim":
        payload["outcome"]["draft"]["claim_ids"] = ["c1"]
        segment["claim_ids"] = ["c2"]
        draft = parse(payload).outcome.draft
        for review in payload["reviews"]:
            review["draft_sha256"] = draft_sha256(draft)
        bind_context(payload)
    else:
        segment["claim_ids"] = ["c1"]
        segment["citations"] = copy.deepcopy(payload["outcome"]["claims"]["claims"][1][
            "citations"])
    outcome = evaluate_package(parse(payload), now=NOW)
    assert outcome.status == "human_review"
    assert outcome.reason in {"draft_coverage_invalid_claim", "draft_coverage_ungrounded"}


def test_old_approve_records_are_readable_but_cannot_authorize_new_delivery():
    payload = synthetic_package()
    del payload["reviews"][0]["facts_check"]
    package = parse(payload)
    assert "facts_check" not in package.model_dump(mode="json")["reviews"][0]
    result = evaluate_package(package, now=NOW)
    assert result.status == "human_review" and result.reason == "draft_coverage_required"


def test_arbitrary_text_cannot_be_covered_as_an_unattributed_formatting_fragment():
    payload = synthetic_package()
    payload["reviews"][0]["facts_check"]["segments"][0].update(claim_ids=[], citations=[])
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review" and result.reason == "draft_coverage_invalid_claim"


def test_citation_must_be_exact_even_when_older_claim_grounding_normalizes_spaces():
    payload = synthetic_package()
    citation = payload["outcome"]["claims"]["claims"][0]["citations"][0]
    citation["quote"] = citation["quote"].replace(" ", "  ")
    seal(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review" and result.reason == "draft_coverage_ungrounded"


@pytest.mark.parametrize("scope", ["sensitive_substance", "uncertain"])
def test_sensitive_substance_cannot_hide_behind_transport_category(scope):
    payload = synthetic_package()
    payload["reviews"][0]["facts_check"].update(
        publication_scope=scope, scope_claim_ids=["c1"])
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review"
    assert result.reason == ("package_requires_review" if scope == "sensitive_substance"
                             else "independent_risk_classification_required")


@pytest.mark.parametrize("invalid", ["empty_scope_claims", "wrong_category"])
def test_transport_scope_requires_both_transport_category_and_supported_effect_ids(invalid):
    payload = synthetic_package(transport_notice=True)
    payload["reviews"][0]["facts_check"].update(
        publication_scope="confirmed_transport_notice", scope_claim_ids=["c2"])
    if invalid == "empty_scope_claims":
        payload["reviews"][0]["facts_check"]["scope_claim_ids"] = []
    else:
        payload["outcome"]["ranking"]["category"] = "city"
        bind_context(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review"
    assert result.reason == "independent_risk_classification_required"


def test_confirmed_transport_change_remains_eligible_independent_of_public_event():
    payload = synthetic_package(transport_notice=True)
    payload["reviews"][0]["facts_check"].update(
        publication_scope="confirmed_transport_notice", scope_claim_ids=["c2"])
    assert evaluate_package(parse(payload), now=NOW).status == "ready"


@pytest.mark.parametrize("category", ["culture", "sport", "city"])
def test_ordinary_local_subjects_do_not_need_a_transport_effect(category):
    payload = synthetic_package(category=category)
    assert evaluate_package(parse(payload), now=NOW).status == "ready"


def test_attributed_subjective_statement_can_be_supported_without_transport_utility():
    payload = synthetic_package(category="culture")
    statement = "Куратор назвал выставку знакомством с историей района."
    payload["evidence"]["body"] = statement
    payload["event"]["documents"][0]["body"] = statement
    claim = payload["outcome"]["claims"]["claims"][0]
    claim["text"] = claim["citations"][0]["quote"] = statement
    payload["outcome"]["draft"].update(
        title="Куратор рассказал о выставке", body=statement)
    seal(payload)
    assert evaluate_package(parse(payload), now=NOW).status == "ready"
