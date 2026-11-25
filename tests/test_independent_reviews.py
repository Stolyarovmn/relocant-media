import json
from datetime import datetime, timedelta

import pytest
from facts_check_helpers import facts_check_for
from test_bgsaobracaj_notices import PUBLISHED, SCHEDULE, ZONE
from test_editorial_package import NOW, event_payload, parse

from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import context_sha256
from relocant_media.ai.provider import StructuredOutputError


class Provider:
    def __init__(self):
        self.calls = []
        self.responses = [
            {"verdict": "approve", "score": 90, "flags": [], "reason": "Подтверждено"},
            {"verdict": "approve", "score": 99, "flags": [], "reason": "Полезно",
             "rubric": {"local_relevance": 4, "practical_value": 3,
                        "timeliness": 3, "clarity": 4}},
        ]

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if (kwargs["task"] == "independent_facts" and isinstance(response, dict)
                and "facts_check" not in response):
            response = {**response,
                        "facts_check": facts_check_for(json.loads(kwargs["prompt"])["package"])}
        return response if isinstance(response, str) else json.dumps(response)


def package():
    payload = event_payload()
    for review in payload["reviews"]:
        review["verdict"] = "hold"
    return parse(payload)


def documents(value):
    return tuple(EvidenceDocument.model_validate(
        d.model_dump(include=set(EvidenceDocument.model_fields))) for d in value.event.documents)


def test_independent_prompts_fetches_and_computed_rubric():
    original = package()
    provider = Provider()
    fetched = []

    def live():
        fetched.append(True)
        return documents(original)

    result = reviewed_package(original, provider, live, now=NOW)
    assert len(fetched) == len(provider.calls) == 2
    assert [r.reviewer_id for r in result.reviews] == ["server-facts", "server-utility"]
    assert result.reviews[1].score == 88
    assert all(r.live_source_checked and r.reviewed_at == NOW for r in result.reviews)
    assert context_sha256(result) == context_sha256(original)
    assert original.reviews[0].verdict == "hold"
    for call in provider.calls:
        payload = json.loads(call["prompt"])
        assert "reviews" not in payload["package"]
        assert payload["context_sha256"] == context_sha256(original)
        assert "verdict" not in call["prompt"]
        assert "now_utc" in payload
        assert "source_road_windows" not in payload
        assert "[тестовый запуск] — обязательная редакционная пометка владельца" in (
            call["system_instruction"])
        assert "Остальной заголовок проверяй по фактам" in call["system_instruction"]
        assert "полное имя, школа, домашний адрес" in call["system_instruction"]
        assert "действующей публичной ориентировки" in call["system_instruction"]
        assert "статус поиска — по двум независимым источникам" in call["system_instruction"]
    assert "utility" not in provider.calls[0]["system_instruction"]
    assert "Один пост — один повод" in provider.calls[1]["system_instruction"]
    assert "Диапазон слов ориентировочный" in provider.calls[1]["system_instruction"]


@pytest.mark.parametrize(("at", "expected"), [
    ((23, 5, 19), ["ended", "ended", "active", "upcoming"]),
    ((23, 6, 0), ["ended", "ended", "ended", "upcoming"]),
    ((24, 6, 0), ["ended", "ended", "ended", "ended"]),
])
def test_explicit_overnight_windows_reach_both_reviewers(at, expected):
    original = package()
    live_document = original.event.documents[0]
    source = live_document.model_copy(update={
        "body": live_document.body + "\n" + SCHEDULE,
        "published_at": PUBLISHED,
    })
    original = original.model_copy(update={
        "event": original.event.model_copy(update={"documents": (source,)}),
    })
    provider = Provider()
    now = datetime(2026, 11, *at, tzinfo=ZONE)
    reviewed_package(original, provider, lambda: documents(original), now=now)
    contexts = [json.loads(call["prompt"])["source_road_windows"] for call in provider.calls]
    assert len(contexts[0]) == 1 and contexts[1] == contexts[0]
    clause = contexts[0][0]
    assert (clause["source_id"], clause["url"], clause["quote"]) == (
        source.source_id, source.url, SCHEDULE,
    )
    assert [window["status"] for window in clause["windows"]] == expected
    assert clause["windows"][-1] == {
        "source_dates": "23./24.11.2026",
        "starts_at": "2026-11-23T22:00:00+01:00",
        "ends_at": "2026-11-24T06:00:00+01:00",
        "status": expected[-1],
    }


def night_notice(body="Когда: в ночь с 23 на 24 ноября 2026 года с 22:00 до 06:00."):
    value = package()
    document = value.event.documents[0].model_copy(update={
        "body": SCHEDULE, "published_at": PUBLISHED,
    })
    claim = value.outcome.claims.claims[0]
    citation = claim.citations[0].model_copy(update={"quote": SCHEDULE})
    claims = value.outcome.claims.model_copy(update={
        "claims": (claim.model_copy(update={
            "text": "Перемежающиеся ночные работы", "citations": (citation,),
        }),),
    })
    draft = value.outcome.draft.model_copy(update={
        "title": "Ночью ограничат движение в Теразийском тоннеле",
        "body": body,
    })
    return value.model_copy(update={
        "evidence": value.evidence.model_copy(update={
            "body": SCHEDULE, "published_at": PUBLISHED,
        }),
        "event": value.event.model_copy(update={"documents": (document,)}),
        "outcome": value.outcome.model_copy(update={"claims": claims, "draft": draft}),
    })


@pytest.mark.parametrize("corrects_coverage", [True, False])
def test_repeat_review_of_same_tunnel_draft_corrects_only_verified_answers(corrects_coverage):
    original = night_notice()
    provider = Provider()
    facts, utility = provider.responses
    broken = facts_check_for(original.model_dump(mode="json"))
    broken["segments"][-1]["citations"][0]["quote"] = SCHEDULE.split(" ")[0]
    invalid = {**facts, "facts_check": broken}
    expired = {**utility, "verdict": "reject", "flags": ["expired_or_passed_shift"]}
    provider.responses = [invalid, facts if corrects_coverage else invalid, expired, utility]
    now = datetime(2026, 11, 23, 7, 17, tzinfo=ZONE)
    audit = []
    result = reviewed_package(original, provider, lambda: documents(original), now=now,
                              audit=audit.append)

    assert [call["task"] for call in provider.calls] == [
        "independent_facts", "independent_facts", "independent_utility", "independent_utility",
    ]
    assert result.outcome == original.outcome and result.event == original.event
    assert result.reviews[0].verdict == ("approve" if corrects_coverage else "hold")
    assert result.reviews[1].verdict == "approve"
    if not corrects_coverage:
        assert result.reviews[0].flags == ("draft_coverage_ungrounded",)
    prompts = [json.loads(call["prompt"]) for call in provider.calls]
    assert prompts[1]["coverage_correction"]["allowed_claims"]["c1"]["citations"][0]["quote"] == (
        SCHEDULE
    )
    assert prompts[3]["timing_correction"]["window"]["status"] == "upcoming"
    assert all(prompt["draft_sha256"] == prompts[0]["draft_sha256"] for prompt in prompts)
    assert [entry["role"] for entry in audit] == [
        "facts_answer_retry", "facts", "utility_answer_retry", "utility",
    ]


@pytest.mark.parametrize(("body", "now", "flags"), [
    ("Ночь с 22 на 23 ноября с 22:00 до 06:00.",
     datetime(2026, 11, 23, 7, 17, tzinfo=ZONE), ["expired_event"]),
    ("Ночь с 23 на 24 ноября 2025 года с 22:00 до 06:00.",
     datetime(2026, 11, 23, 7, 17, tzinfo=ZONE), ["expired_event"]),
    ("Ночь с 23 на 24 ноября с 22:00 до 06:00.",
     datetime(2026, 11, 24, 6, 0, tzinfo=ZONE), ["expired_event"]),
    ("Ночь с 23 на 24 ноября с 22:00 до 06:00.",
     datetime(2026, 11, 23, 7, 17, tzinfo=ZONE), ["expired_event", "неверное направление"]),
])
def test_expiry_retry_requires_matching_live_window_and_no_other_objection(body, now, flags):
    original = night_notice(body)
    provider = Provider()
    provider.responses[1].update(verdict="reject", flags=flags)
    result = reviewed_package(original, provider, lambda: documents(original), now=now)
    assert len(provider.calls) == 2
    assert result.reviews[1].verdict == "reject"
    assert result.reviews[1].flags == tuple(flags)


@pytest.mark.parametrize("changed_fetch", [1, 2])
def test_live_change_fails_before_approval(changed_fetch):
    original = package()
    provider = Provider()
    count = 0

    def live():
        nonlocal count
        count += 1
        docs = documents(original)
        if count == changed_fetch:
            docs = (docs[0].model_copy(update={"body": "Изменено"}),)
        return docs

    with pytest.raises(ValueError, match="live evidence changed"):
        reviewed_package(original, provider, live, now=NOW)
    assert len(provider.calls) == changed_fetch - 1


@pytest.mark.parametrize("bad", ["not json", {"verdict": "approve", "score": True,
    "flags": [], "reason": "Ошибка"}, {"verdict": "approve", "score": 90,
    "flags": "none", "reason": "Ошибка"}])
def test_invalid_response_fails_closed(bad):
    original = package()
    provider = Provider()
    provider.responses[0] = bad
    with pytest.raises(StructuredOutputError):
        reviewed_package(original, provider, lambda: documents(original), now=NOW)


def test_flags_are_preserved_and_missing_rubric_fails():
    original = package()
    provider = Provider()
    provider.responses[0]["flags"] = ["Неясная дата"]
    result = reviewed_package(original, provider, lambda: documents(original), now=NOW)
    assert result.reviews[0].flags == ("Неясная дата",)
    provider = Provider()
    del provider.responses[1]["rubric"]
    with pytest.raises(StructuredOutputError):
        reviewed_package(original, provider, lambda: documents(original), now=NOW)


def test_utc_normalization_and_naive_clock():
    original = package()
    docs = documents(original)
    if docs[0].published_at is not None:
        from datetime import timezone
        docs = (docs[0].model_copy(update={"published_at": docs[0].published_at.astimezone(
            timezone(timedelta(hours=3)))}),)
    reviewed_package(original, Provider(), lambda: docs, now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        reviewed_package(original, Provider(), lambda: docs, now=NOW.replace(tzinfo=None))
