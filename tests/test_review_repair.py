import json

import pytest
from test_independent_reviews import Provider, documents, package

from relocant_media.ai.package import context_sha256, draft_sha256
from relocant_media.ai.provider import StructuredOutputError
from relocant_media.ai.review_repair import review_with_repairs


def initial():
    value = package()
    draft = value.outcome.draft.model_copy(update={
        "title": "[тестовый запуск] " + value.outcome.draft.title,
    })
    return value.model_copy(update={"outcome": value.outcome.model_copy(update={"draft": draft})})


def reviews(hold=False):
    result = Provider().responses
    if hold:
        result[0].update(verdict="hold", flags=["Неточность"], reason="Уточни заголовок")
    return result


def repaired(value):
    return {**value.outcome.draft.model_dump(mode="json"),
            "title": "[тестовый запуск] Уточнённый заголовок"}


def test_approved_draft_requires_no_repair():
    value, provider = initial(), Provider()
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert len(provider.calls) == 2
    assert result.outcome == value.outcome


def test_repair_gets_both_fresh_reviews_and_preserves_all_other_fields():
    value, provider, audit = initial(), Provider(), []
    provider.responses = [*reviews(True), repaired(value), *reviews()]
    result = review_with_repairs(value, provider, lambda: documents(value), audit=audit.append)
    assert [c["task"] for c in provider.calls] == [
        "independent_facts", "independent_utility", "repair_draft",
        "independent_facts", "independent_utility",
    ]
    original = value.model_dump(exclude={"reviews"})
    final = result.model_dump(exclude={"reviews"})
    del original["outcome"]["draft"], final["outcome"]["draft"]
    assert original == final
    assert all(r.context_sha256 == context_sha256(result) and
               r.draft_sha256 == draft_sha256(result.outcome.draft) for r in result.reviews)
    assert context_sha256(result) != context_sha256(value)
    repair_prompt = json.loads(provider.calls[2]["prompt"])
    assert repair_prompt["review_comments"][0]["reason"] == "Уточни заголовок"
    assert [e["repair_attempt"] for e in audit] == [0, 0, 1, 1]


def test_two_repairs_maximum_and_last_hold_survives():
    value, provider = initial(), Provider()
    provider.responses = [*reviews(True), repaired(value), *reviews(True),
                          repaired(value), *reviews(True)]
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert len(provider.calls) == 8
    assert result.reviews[0].verdict == "hold"
    assert result.reviews[0].flags == ("Неточность",)


@pytest.mark.parametrize("mutation", ["unknown_claim", "duplicate", "prefix", "extra", "invalid"])
def test_invalid_repairs_fail_closed_without_final_approval(mutation):
    value, provider = initial(), Provider()
    draft = repaired(value)
    if mutation == "unknown_claim":
        draft["claim_ids"] = ["unsupported"]
    elif mutation == "duplicate":
        draft["claim_ids"] *= 2
    elif mutation == "prefix":
        draft["title"] = "Без пометки"
    elif mutation == "extra":
        draft["reviews"] = "approve"
    provider.responses = [*reviews(True), "invalid" if mutation == "invalid" else draft]
    with pytest.raises(StructuredOutputError):
        review_with_repairs(value, provider, lambda: documents(value))
    assert len(provider.calls) == 3


def test_changed_evidence_after_repair_fails_closed():
    value, provider = initial(), Provider()
    provider.responses = [*reviews(True), repaired(value), *reviews()]
    count = 0

    def live():
        nonlocal count
        count += 1
        docs = documents(value)
        return docs if count <= 2 else (docs[0].model_copy(update={"body": "Изменено"}),)

    with pytest.raises(ValueError, match="live evidence changed"):
        review_with_repairs(value, provider, live)
    assert len(provider.calls) == 3


@pytest.mark.parametrize("limit", [-1, 3, True])
def test_unbounded_repairs_rejected(limit):
    value = initial()
    with pytest.raises(ValueError, match="max_repairs"):
        review_with_repairs(value, Provider(), lambda: documents(value), max_repairs=limit)
