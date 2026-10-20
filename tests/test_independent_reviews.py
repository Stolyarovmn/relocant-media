import json
from datetime import timedelta

import pytest
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
    assert "utility" not in provider.calls[0]["system_instruction"]


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
