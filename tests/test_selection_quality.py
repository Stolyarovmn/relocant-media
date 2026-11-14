import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from test_city_editorial_profile import second_city_profile
from test_editorial_package import NOW, package_payload, parse
from test_editorial_pipeline import (
    FakeProvider,
    _claims,
    _document,
    _draft,
    _ranking,
    _verification,
)
from test_independent_reviews import Provider, documents, package

from relocant_media.ai.audience_policy import PROFILE_AUDIENCE_MISMATCH_FLAG
from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.models import (
    EvidenceDocument,
    PublicationStatus,
    SelectionBrief,
    SelectionCitation,
)
from relocant_media.ai.package import canonical_json, context_sha256
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.selection import selection_reason
from relocant_media.domain.city_profile import CityEditorialProfile, load_city_profile
from relocant_media.domain.freshness import publication_date_context


def brief(
    profile: CityEditorialProfile, document: EvidenceDocument | None = None,
) -> SelectionBrief:
    document = document or _document()
    return SelectionBrief(
        policy_version=profile.policy_version, profile_sha256=profile.sha256,
        audience="Жители района отключения", benefit="Можно подготовиться к отключению воды",
        novelty="Источник объявил конкретный интервал отключения",
        why_now="Подготовиться нужно до начала указанного отключения",
        rubric_id="city_changes", locality="city", value_kind="action",
        evidence_refs=(SelectionCitation(source_id=document.source_id, url=document.url,
                                          quote=document.body),),
    )


class CapturingProvider(FakeProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.requests: list[dict[str, Any]] = []

    def generate_json(self, **kwargs: Any) -> str:
        self.requests.append(kwargs)
        return super().generate_json(**kwargs)


class CorrectedRankingProvider(CapturingProvider):
    def __init__(self, responses, corrected):
        super().__init__(responses)
        self.corrected = corrected

    def generate_json(self, **kwargs: Any) -> str:
        if kwargs["task"] == "rank" and "correction" in json.loads(kwargs["prompt"]):
            self._responses["rank"] = self.corrected
        return super().generate_json(**kwargs)


def provider_for(profile, *, category="utilities", selection=None):
    return CapturingProvider({
        "rank": _ranking(category).model_copy(update={"selection": selection or brief(profile)}),
        "claims": _claims(), "verify": _verification(), "draft": _draft(),
    })


def test_legacy_rank_and_package_keep_exact_serialized_contract() -> None:
    old_ranking = {
        "should_process": True, "relevance": 0.98, "category": "transport",
        "confidence": 0.99, "rationale": "Useful city service information.",
    }
    assert _ranking("transport").model_dump(mode="json") == old_ranking
    legacy = parse(package_payload())
    expected = legacy.model_dump(mode="json", exclude={"reviews"})
    expected["outcome"]["ranking"] = old_ranking
    assert context_sha256(legacy) == hashlib.sha256(canonical_json(expected).encode()).hexdigest()
    assert "selection" not in legacy.model_dump(mode="json")["outcome"]["ranking"]


def test_profile_selection_passes_verified_warning_and_recent_history() -> None:
    profile = load_city_profile("belgrade")
    provider = provider_for(profile)
    history = [{"id": "45", "title": "Матч завершён", "body": "Итог матча 103:73"}]
    result = EditorialPipeline(provider, city_profile=profile).run(
        city="belgrade", documents=(_document(),), recent_publications=history,
    )
    assert result.status == PublicationStatus.READY
    assert provider.calls == ["rank", "claims", "verify", "draft"]
    rank = provider.requests[0]
    assert json.loads(rank["prompt"])["recent_publications"] == history
    assert "Сам перевод местной новости" in rank["system_instruction"]
    assert "совпадение одного счёта" in rank["system_instruction"]
    assert "предполагаемого низкого охвата" in rank["system_instruction"]


def test_new_profile_requires_selection_before_claims_or_draft() -> None:
    profile = load_city_profile("belgrade")
    provider = FakeProvider({"rank": _ranking()})
    result = EditorialPipeline(provider, city_profile=profile).run(
        city="belgrade", documents=(_document(),),
    )
    assert result.status == PublicationStatus.HUMAN_REVIEW
    assert result.reason == "selection_brief_missing"
    assert provider.calls == ["rank"]


@pytest.mark.parametrize("mutation,reason", [
    ({"profile_sha256": "0" * 64}, "selection_profile_mismatch"),
    ({"policy_version": "unknown-v1"}, "selection_profile_mismatch"),
    ({"rubric_id": "missing"}, "selection_rubric_mismatch"),
    ({"rubric_id": "weekend"}, "selection_rubric_mismatch"),
    ({"evidence_refs": (SelectionCitation(source_id="city_beograd_news",
        url="https://www.beograd.rs/example.html", quote="Выдуманное последствие"),)},
     "selection_ungrounded_evidence"),
])
def test_unsupported_selection_stops_before_expensive_stages(mutation, reason) -> None:
    profile = load_city_profile("belgrade")
    provider = provider_for(profile, selection=brief(profile).model_copy(update=mutation))
    result = EditorialPipeline(provider, city_profile=profile).run(
        city="belgrade", documents=(_document(),),
    )
    assert result.reason == reason
    assert result.status == PublicationStatus.HUMAN_REVIEW
    expected_calls = ["rank", "rank"] if reason == "selection_ungrounded_evidence" else ["rank"]
    assert provider.calls == expected_calls


def test_water_notice_retries_selection_with_verbatim_evidence() -> None:
    profile = load_city_profile("belgrade")
    document = _document()
    good = _ranking().model_copy(update={"selection": brief(profile, document)})
    citation = good.selection.evidence_refs[0].model_copy(update={
        "quote": document.body + " Raspored auto cisterni:",
    })
    bad = good.model_copy(update={"selection": good.selection.model_copy(update={
        "evidence_refs": (citation,),
    })})
    provider = CorrectedRankingProvider(
        {"rank": bad, "claims": _claims(), "verify": _verification(), "draft": _draft()}, good,
    )
    result = EditorialPipeline(provider, city_profile=profile).run(
        city="belgrade", documents=(document,),
    )
    assert result.status == PublicationStatus.READY
    assert provider.calls == ["rank", "rank", "claims", "verify", "draft"]
    assert "no merged passages" in json.loads(provider.requests[1]["prompt"])["correction"]


@pytest.mark.parametrize("corrected", [True, False])
def test_air_serbia_cancellation_gets_a_second_look_or_review(corrected) -> None:
    profile = load_city_profile("belgrade")
    document = _document().model_copy(update={
        "title": "Air Serbia obustavlja letove za Guangdžou posle 27. novembra",
        "body": "Er Srbija povukla je iz prodaje direktne letove između Beograda i Guangdžoua.",
    })
    rejected = _ranking("economy").model_copy(update={"should_process": False})
    selected = _ranking("transport").model_copy(update={"selection": brief(profile, document)})
    provider = CorrectedRankingProvider(
        {"rank": rejected, "claims": _claims(document.body), "verify": _verification(),
         "draft": _draft().model_copy(update={
             "title": "Air Serbia сняла с продажи рейсы в Гуанчжоу",
             "body": "Из продажи убрали прямые рейсы между Белградом и Гуанчжоу.",
         })},
        selected if corrected else rejected,
    )
    result = EditorialPipeline(provider, city_profile=profile).run(
        city="belgrade", documents=(document,),
    )
    assert provider.calls[:2] == ["rank", "rank"]
    assert "пассажиров" in json.loads(provider.requests[1]["prompt"])["correction"]
    if corrected:
        assert result.status == PublicationStatus.READY
        assert result.ranking.category == "transport"
    else:
        assert result.status == PublicationStatus.HUMAN_REVIEW
        assert result.reason == "local_flight_change_requires_review"
        assert provider.calls == ["rank", "rank"]


def test_rejected_high_relevance_does_not_enter_old_borderline_fallback() -> None:
    profile = load_city_profile("belgrade")
    provider = FakeProvider({"rank": _ranking().model_copy(update={"should_process": False})})
    result = EditorialPipeline(provider, city_profile=profile).run(
        city="belgrade", documents=(_document(),),
    )
    assert result.status == PublicationStatus.SKIPPED
    assert provider.calls == ["rank"]


def test_new_profile_retains_high_risk_human_review() -> None:
    profile = load_city_profile("belgrade")
    result = EditorialPipeline(provider_for(profile, category="immigration"),
                               city_profile=profile).run(city="belgrade", documents=(_document(),))
    assert result.reason == "policy_requires_human_review"


def test_meaningful_city_story_does_not_require_personal_action() -> None:
    profile = load_city_profile("belgrade")
    selected = brief(profile).model_copy(update={
        "rubric_id": "city_life", "value_kind": "connection",
        "benefit": "Понять, как жители сохраняют местную культурную традицию",
    })
    result = EditorialPipeline(provider_for(profile, category="city", selection=selected),
                               city_profile=profile).run(city="belgrade", documents=(_document(),))
    assert result.status == PublicationStatus.READY


def test_country_scope_and_evergreen_are_explicit() -> None:
    profile = load_city_profile("belgrade")
    profile = profile.model_copy(update={
        "geography": profile.geography.model_copy(update={"allow_country_scope": False}),
    })
    ranking = _ranking().model_copy(update={
        "selection": brief(profile).model_copy(update={"locality": "country"}),
    })
    assert selection_reason(ranking, profile, (_document(),)) == (
        "selection_geography_outside_profile"
    )
    ranking = _ranking().model_copy(update={
        "selection": brief(profile).model_copy(update={"value_kind": "understanding"}),
    })
    assert selection_reason(ranking, profile, (_document(),), evergreen=True) == (
        "selection_guide_requires_practical_value"
    )


def test_second_country_uses_its_profile_and_calendar() -> None:
    profile = second_city_profile()
    provider = provider_for(profile)
    document = _document().model_copy(update={
        "published_at": datetime(2026, 11, 11, 22, tzinfo=UTC), "published_at_precision": "instant",
    })
    EditorialPipeline(provider, city_profile=profile).run(
        city="tbilisi", documents=(document,), now=datetime(2026, 11, 13, 15, tzinfo=UTC),
    )
    for request in (provider.requests[0], provider.requests[-1]):
        assert "Белград" not in request["system_instruction"]
        assert "Сербия" not in request["system_instruction"]
        assert json.loads(request["prompt"])["city_profile"]["country_code"] == "GE"
    dates = json.loads(provider.requests[-1]["prompt"])["source_local_dates"][0]
    assert dates["timezone"] == "Asia/Tbilisi"
    assert dates["published_on_local"] == "2026-11-12"
    assert dates["tomorrow_local"] == "2026-11-13"
    draft_request = provider.requests[-1]
    assert json.loads(draft_request["prompt"])["writing_calendar"] == {
        "timezone": "Asia/Tbilisi", "prepared_at_local": "2026-11-13T19:00+04:00",
        "day_of_week": "пт", "daypart": "вечер",
    }
    assert "40–100 слов" in draft_request["system_instruction"]
    assert "точные адреса или подтверждённая граница" in draft_request["system_instruction"]
    with pytest.raises(ValueError, match="не совпадает"):
        EditorialPipeline(provider, city_profile=profile).run(
            city="belgrade", documents=(_document(),),
        )


def test_legacy_publication_date_keys_are_unchanged() -> None:
    moment = datetime(2026, 11, 11, 22, tzinfo=UTC)
    assert "published_at_belgrade" in publication_date_context(moment, "instant")
    local = publication_date_context(moment, "day", timezone_name="Asia/Tbilisi")
    assert local["published_at_local"] is None
    assert local["published_on_local"] == "2026-11-12"


def profiled_package(profile):
    original = package()
    original = original.model_copy(update={"city": profile.city_id})
    ranking = original.outcome.ranking.model_copy(update={
        "selection": brief(profile, documents(original)[0]),
    })
    return original.model_copy(update={"outcome": original.outcome.model_copy(
        update={"ranking": ranking})})


def test_independent_utility_enforces_higher_value_and_preserves_hash() -> None:
    profile = load_city_profile("belgrade")
    original = profiled_package(profile)
    provider = Provider()
    provider.responses[1]["rubric"]["practical_value"] = 2
    result = reviewed_package(original, provider, lambda: documents(original), now=NOW,
                              city_profile=profile)
    assert result.reviews[1].verdict == "hold"
    assert PROFILE_AUDIENCE_MISMATCH_FLAG in result.reviews[1].flags
    assert context_sha256(original) == context_sha256(result)
    assert all(review.context_sha256 == context_sha256(original) for review in result.reviews)
    assert "Диапазон слов ориентировочный" in provider.calls[1]["system_instruction"]
    assert "writing_calendar" in json.loads(provider.calls[1]["prompt"])


def test_review_reads_profile_for_new_packages_and_rejects_changed_configuration() -> None:
    profile = load_city_profile("belgrade")
    original = profiled_package(profile)
    result = reviewed_package(original, Provider(), lambda: documents(original), now=NOW)
    assert result.reviews[1].verdict == "approve"
    changed = profile.model_copy(update={"policy_version": "different-v1"})
    provider = Provider()
    with pytest.raises(ValueError, match="selection_profile_mismatch"):
        reviewed_package(original, provider, lambda: documents(original), city_profile=changed)
    assert not provider.calls


def test_second_country_review_has_no_belgrade_calendar_or_audience() -> None:
    profile = second_city_profile()
    original = profiled_package(profile)
    provider = Provider()
    reviewed_package(original, provider, lambda: documents(original), city_profile=profile, now=NOW)
    for call in provider.calls:
        assert "Белград" not in call["system_instruction"]
        assert "Сербия" not in call["system_instruction"]
        assert "Europe/Belgrade" not in call["system_instruction"]
        payload = json.loads(call["prompt"])
        assert payload["source_local_dates"][0]["timezone"] == "Asia/Tbilisi"
