import json
from datetime import UTC, datetime

import pytest
from test_bgsaobracaj_notices import PUBLISHED, SCHEDULE, ZONE
from test_independent_reviews import Provider, documents, package

from relocant_media.ai.channel_style import CHANNEL_STYLE_VERSION
from relocant_media.ai.package import context_sha256, draft_sha256, evaluate_package
from relocant_media.ai.provider import StructuredOutputError
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.domain.freshness import draft_date_reason


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


def dated_water_notice():
    value = initial()
    date_fields = {"published_at": datetime(2026, 11, 22, 23, tzinfo=UTC),
                   "published_at_precision": "day"}
    document = value.event.documents[0]
    evidence = value.evidence.model_copy(update=date_fields)
    draft = value.outcome.draft.model_copy(update={
        "body": "23 ноября с 08:00 до 18:00 часть Звездары останется без воды.",
    })
    relative = draft.model_copy(update={"body": "Сегодня, " + draft.body})
    ranking = value.outcome.ranking.model_copy(update={"category": "utilities"})
    value = value.model_copy(update={
        "outcome": value.outcome.model_copy(update={"draft": relative, "ranking": ranking}),
        "event": value.event.model_copy(update={"documents": (
            document.model_copy(update=date_fields),
        )}),
        "evidence": evidence,
    })
    return value, draft


def test_relative_day_is_repaired_before_reviews_even_if_a_calendar_date_is_present():
    value, corrected = dated_water_notice()
    provider, audit, fetches = Provider(), [], []
    provider.responses = [corrected.model_dump(mode="json"), *reviews()]

    def live():
        fetches.append(len(provider.calls))
        return documents(value)

    result = review_with_repairs(value, provider, live, audit=audit.append)

    assert [call["task"] for call in provider.calls] == [
        "repair_draft", "independent_facts", "independent_utility",
    ]
    assert fetches == [0, 1, 2]
    assert audit[0]["role"] == "date_guard" and audit[0]["repair_attempt"] == 0
    repair_prompt = json.loads(provider.calls[0]["prompt"])
    assert "«сегодня»" in repair_prompt["review_comments"][0]["instruction"]
    assert repair_prompt["source_local_dates"][0]["published_on_belgrade"] == "2026-11-23"
    assert repair_prompt["source_local_dates"][0]["published_at_belgrade"] is None
    for call in provider.calls[1:]:
        assert json.loads(call["prompt"])["package"]["outcome"]["draft"]["body"] == corrected.body
    assert result.outcome.draft == corrected
    assert result.event == value.event and result.evidence == value.evidence
    assert all(r.context_sha256 == context_sha256(result)
               and r.draft_sha256 == draft_sha256(corrected) for r in result.reviews)
    evaluated = evaluate_package(result)
    assert (evaluated.status, evaluated.reason) == ("human_review", "package_requires_review")


@pytest.mark.parametrize("fails_at", [0, 1])
def test_early_date_repair_requires_unchanged_sources_before_repair_and_review(fails_at):
    value, corrected = dated_water_notice()
    provider = Provider()
    provider.responses = [corrected.model_dump(mode="json"), *reviews()]

    def live():
        doc, = documents(value)
        if len(provider.calls) == fails_at:
            doc = doc.model_copy(update={"body": "Изменено"})
        return (doc,)

    with pytest.raises(ValueError, match="live evidence changed"):
        review_with_repairs(value, provider, live)
    assert len(provider.calls) == fails_at


def test_date_repair_uses_the_same_budget_and_preserves_the_publication_guard():
    value, corrected = dated_water_notice()
    provider = Provider()
    still_relative = corrected.model_copy(update={"body": "Завтра, " + corrected.body})
    provider.responses = [still_relative.model_dump(mode="json"), *reviews()]
    result = review_with_repairs(value, provider, lambda: documents(value), max_repairs=1)
    assert [call["task"] for call in provider.calls] == [
        "repair_draft", "independent_facts", "independent_utility",
    ]
    assert draft_date_reason(result.outcome.draft.title, result.outcome.draft.body)
    assert evaluate_package(result).reason == "draft_requires_absolute_event_date"


def test_unchanged_early_date_repair_does_not_repeat_the_request():
    value, _ = dated_water_notice()
    provider = Provider()
    provider.responses = [value.outcome.draft.model_dump(mode="json"), *reviews()]
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert [call["task"] for call in provider.calls] == [
        "repair_draft", "independent_facts", "independent_utility",
    ]
    assert evaluate_package(result).reason == "draft_requires_absolute_event_date"


def test_repaired_draft_discards_old_factual_coverage_before_new_review(monkeypatch):
    from relocant_media.ai import review_repair

    value, provider = initial(), Provider()
    provider.responses = [*reviews(True), repaired(value), *reviews()]
    actual_review = review_repair.reviewed_package
    incoming = []

    def capture(package, *args, **kwargs):
        incoming.append(package)
        if len(incoming) == 2:
            assert all(review.facts_check is None for review in package.reviews)
            assert all(review.verdict == "hold" for review in package.reviews)
        return actual_review(package, *args, **kwargs)

    monkeypatch.setattr(review_repair, "reviewed_package", capture)
    result = review_repair.review_with_repairs(value, provider, lambda: documents(value))
    assert len(incoming) == 2 and len(provider.calls) == 5
    assert result.reviews[0].facts_check is not None
    assert result.reviews[0].facts_check.segments[0].text == result.outcome.draft.title


def test_repair_uses_same_overnight_window_statuses_as_reviewers(monkeypatch):
    from relocant_media.ai import review_repair

    value = initial()
    source = value.event.documents[0]
    source = source.model_copy(update={
        "body": source.body + "\n" + SCHEDULE,
        "published_at": PUBLISHED,
    })
    value = value.model_copy(update={
        "event": value.event.model_copy(update={"documents": (source,)}),
    })
    provider = Provider()
    provider.responses = [*reviews(True), repaired(value), *reviews()]
    actual_review = review_repair.reviewed_package

    def review(*args, **kwargs):
        return actual_review(*args, now=datetime(2026, 11, 23, 5, 19, tzinfo=ZONE), **kwargs)

    monkeypatch.setattr(review_repair, "reviewed_package", review)
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert [call["task"] for call in provider.calls].count("repair_draft") == 1
    prompts = [json.loads(call["prompt"]) for call in provider.calls]
    assert prompts[2]["source_road_windows"] == prompts[0]["source_road_windows"]
    assert "напечатанный интервал" in provider.calls[2]["system_instruction"]
    assert result.event == value.event


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
    assert final["outcome"].pop("writing_matrix_version") == CHANNEL_STYLE_VERSION
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
                          {**repaired(value),"title":"[тестовый запуск] Ещё одно уточнение"},
                          *reviews(True)]
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert len(provider.calls) == 8
    assert result.reviews[0].verdict == "hold"
    assert result.reviews[0].flags == ("Неточность",)


def test_unchanged_repair_cannot_reroll_failed_reviews():
    value,provider=initial(),Provider()
    provider.responses=[*reviews(True),value.outcome.draft.model_dump(mode="json"),*reviews()]
    result=review_with_repairs(value,provider,lambda: documents(value))
    assert len(provider.calls)==3
    assert result.reviews[0].verdict=="hold"


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
