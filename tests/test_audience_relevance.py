import pytest
from test_editorial_pipeline import FakeProvider, _document
from test_independent_reviews import Provider, documents, package

from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.domain.audience import audience_relevance_reason

SOURCE_TITLE = "Одржана прва радионица у оквиру пројекта „Оснажена жена – снажна заједница“"
SOURCE_BODY = (
    "Бесплатне радионице су намењене женама. Друга радионица, на тему примера добре "
    "праксе и јавних набавки, биће одржана 30. октобра у Галерији ’73.")


def test_real_editorial_miss_is_rejected_before_any_ai_call():
    provider = FakeProvider({})
    document = _document().model_copy(update={"title": SOURCE_TITLE, "body": SOURCE_BODY})
    result = EditorialPipeline(provider).run(city="belgrade", documents=(document,))
    assert result.status == "skipped"
    assert result.reason == "audience_niche_procurement_training"
    assert not provider.calls and result.draft is None


@pytest.mark.parametrize(("title", "body"), [
    ("Мастерская по государственным закупкам", "30 октября, бесплатно"),
    ("Radionica o javnim nabavkama", "30. oktobra"),
    ("Public procurement training", "Free admission"),
])
def test_professional_training_is_not_general_interest(title, body):
    assert audience_relevance_reason(title, body) == "audience_niche_procurement_training"


@pytest.mark.parametrize(("title", "body"), [
    ("Занятия сербским для новичков", "Бесплатная мастерская для женщин, 30 октября"),
    ("Галерея ’73: концерт", "Состоится 30 октября, вход бесплатный"),
    ("Закупки автобусов", "Город объявил государственные закупки автобусов"),
    ("Изменения транспорта", "Объезд до 30 октября, радионица закрыта"),
])
def test_no_blanket_ban_on_workshops_gender_venues_or_transit(title, body):
    assert audience_relevance_reason(title, body) is None


@pytest.mark.parametrize("dimension", ["local_relevance", "practical_value"])
def test_high_total_cannot_compensate_for_poor_audience_fit(dimension):
    value, provider, audit = package(), Provider(), []
    provider.responses[1]["rubric"] = dict(local_relevance=4, practical_value=4,
                                            timeliness=4, clarity=4)
    provider.responses[1]["rubric"][dimension] = 2
    result = reviewed_package(value, provider, lambda: documents(value), audit=audit.append)
    review = result.reviews[1]
    assert review.score == 88 and review.verdict == "hold" and review.flags
    assert audit[1]["verdict"] == "hold"
    assert "кому из наших читателей" in provider.calls[1]["system_instruction"]
    assert "ограничения участия" in provider.calls[0]["system_instruction"]


def test_audience_rejection_does_not_spend_calls_on_cosmetic_repairs():
    value, provider = package(), Provider()
    provider.responses[1]["rubric"]["local_relevance"] = 1
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert result.reviews[1].verdict == "hold"
    assert [c["task"] for c in provider.calls] == ["independent_facts", "independent_utility"]
