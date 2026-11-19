"""Граница уведомлений и советов должна достигать автора и обеих проверок."""

import pytest
from test_editorial_package import NOW, bind_context, event_payload, parse
from test_editorial_pipeline import (
    FakeProvider,
    _claims,
    _document,
    _draft,
    _ranking,
    _verification,
)
from test_independent_reviews import Provider, documents, package

from relocant_media.ai.channel_style import (
    PROCEDURAL_GUIDANCE_POLICY,
    writing_brief,
    writing_instruction,
)
from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.package import evaluate_package
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.domain.city_profile import load_city_profile


class CaptureProvider(FakeProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.instructions = {}

    def generate_json(self, **kwargs):
        self.instructions[kwargs["task"]] = kwargs["system_instruction"]
        return super().generate_json(**kwargs)


@pytest.mark.parametrize("evergreen", [False, True])
def test_news_and_evergreen_authors_receive_the_same_procedural_boundary(evergreen):
    provider = CaptureProvider({
        "rank": _ranking("city"), "claims": _claims(),
        "verify": _verification(), "draft": _draft(),
    })
    outcome = EditorialPipeline(provider).run(
        city="belgrade", documents=(_document(),), evergreen=evergreen, now=NOW,
    )
    assert outcome.draft is not None
    assert PROCEDURAL_GUIDANCE_POLICY in provider.instructions["draft"]
    if evergreen:
        assert "processing, not approval" in provider.instructions["rank"]
        assert "Не заполняй отсутствующие документы" in provider.instructions["draft"]


def test_subscription_brief_carries_the_boundary_and_does_not_require_invented_steps():
    brief = writing_brief(load_city_profile("belgrade"), NOW)
    assert brief["procedural_guidance"] == PROCEDURAL_GUIDANCE_POLICY
    assert "Не заполняй отсутствующие документы" in writing_instruction("city", evergreen=True)


@pytest.mark.parametrize("blocking_role", ["facts", "utility"])
def test_either_independent_role_can_hold_an_unsupported_procedural_claim(blocking_role):
    original = package()
    provider = Provider()
    index = 0 if blocking_role == "facts" else 1
    provider.responses[index].update(
        verdict="hold", flags=["Не подтверждена применимость процедуры к читателю"],
        reason="Официальное уведомление не подтверждает право каждого на услугу.",
    )
    reviewed = reviewed_package(original, provider, lambda: documents(original), now=NOW)
    assert all(PROCEDURAL_GUIDANCE_POLICY in c["system_instruction"] for c in provider.calls)
    assert evaluate_package(reviewed, now=NOW).status == "human_review"
    assert reviewed.reviews[index].flags == (
        "Не подтверждена применимость процедуры к читателю",
    )


@pytest.mark.parametrize("category", ["banking", "law", "immigration", "tax", "medicine"])
def test_official_notice_with_perfect_reviews_does_not_expand_sensitive_categories(category):
    payload = event_payload()
    payload["outcome"]["ranking"]["category"] = category
    bind_context(payload)
    assert evaluate_package(parse(payload), now=NOW).status == "human_review"


def test_fully_reviewed_low_risk_notice_keeps_existing_eligibility():
    assert evaluate_package(parse(event_payload()), now=NOW).status == "ready"
