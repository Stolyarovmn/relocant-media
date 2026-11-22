"""#312: проба моделей читает снимок и ничего не пишет и не отправляет."""
import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_channel_delivery import reviewed as reviewed_fixture

from relocant_media.ai.models import EditorialOutcome, PublicationStatus, RankingResult
from relocant_media.ops import model_trial
from relocant_media.storage.models import EditorialRun, Post

reviewed = reviewed_fixture
MODELS = {"default": "gemini-3.5-flash-lite", "draft": "gemini-3.8-flash",
          "review": "gemini-3.6-flash"}


def _sent(session, run, **outcome):
    run = session.get(EditorialRun, run.raw_item_id)
    run.delivery_status = "sent"
    run.outcome = {**run.outcome, **outcome}
    post = session.get(Post, run.post_id)
    post.telegram_message_id = 77
    post.published_at = datetime(2026, 11, 21, 10, tzinfo=UTC)
    session.commit()
    return run


class FakeProvider:
    def __init__(self, model):
        self.model = model

    def generate_json(self, **kwargs):
        return "{}"


@pytest.fixture
def fakes(monkeypatch):
    calls = []

    class Pipeline:
        def __init__(self, provider, city_profile=None):
            self.provider = provider

        def run(self, *, city, documents, now):
            self.provider.generate_json(task="draft", system_instruction="", prompt="",
                                        response_schema={})
            calls.append(("draft", now))
            return EditorialOutcome(status=PublicationStatus.SKIPPED, reason="not_local",
                ranking=RankingResult(should_process=False, relevance=0, category="city",
                                      confidence=1, rationale="x"), confidence=1)

    def review(package, provider, live, *, now, audit):
        assert all(item.verdict == "hold" for item in package.reviews)
        assert live() == tuple(package.event.documents)
        for role in ("facts", "utility"):
            provider.generate_json(task=f"independent_{role}", system_instruction="",
                                   prompt="", response_schema={})
            audit({"role": role, "verdict": "hold", "computed_score": 40,
                   "flags": ["Дата не указана"], "reason": "Нет даты"})
        calls.append(("review", now))

    monkeypatch.setattr(model_trial, "EditorialPipeline", Pipeline)
    monkeypatch.setattr(model_trial, "reviewed_package", review)
    return calls


def test_trial_compares_published_post_without_writing_state(reviewed, fakes):
    session, run = reviewed
    _sent(session, run)
    database = session.get_bind().url.database
    before = hashlib.sha256(open(database, "rb").read()).hexdigest()
    result = model_trial.run_trial(database, MODELS, FakeProvider, limit=5)
    assert hashlib.sha256(open(database, "rb").read()).hexdigest() == before
    [item] = result["items"]
    assert item["published"]["title"] and item["candidate_draft"]["status"] == "skipped"
    assert [r["verdict"] for r in item["candidate_reviews_of_published"]] == ["hold", "hold"]
    assert [(c["task"], c["model"]) for c in item["calls"]] == [
        ("draft", "gemini-3.8-flash"), ("independent_facts", "gemini-3.6-flash"),
        ("independent_utility", "gemini-3.6-flash")]
    # Модель видит время исходной публикации, а не текущее.
    assert {when for _, when in fakes} == {datetime(2026, 11, 21, 10, tzinfo=UTC)}
    summary = result["summary"]
    assert summary["published_posts_not_approved_by_candidate_reviews"] == 1
    assert summary["by_role_and_model"]["draft:gemini-3.8-flash"]["calls"] == 1
    assert "| review:gemini-3.6-flash | 2 | 0 |" in model_trial.markdown(result)


def test_reader_submissions_and_unsent_posts_are_never_sent_to_models(reviewed, fakes):
    session, run = reviewed
    database = session.get_bind().url.database
    assert model_trial.run_trial(database, MODELS, FakeProvider, limit=5)["items"] == []
    _sent(session, run, _reader_submission_id=5)
    assert model_trial.run_trial(database, MODELS, FakeProvider, limit=5)["items"] == []
    assert fakes == []


def test_model_failure_is_recorded_per_item(reviewed, fakes, monkeypatch):
    session, run = reviewed
    _sent(session, run)

    class Broken:
        def __init__(self, provider, city_profile=None):
            pass

        def run(self, **kwargs):
            raise TimeoutError("secret response body")

    monkeypatch.setattr(model_trial, "EditorialPipeline", Broken)
    database = session.get_bind().url.database
    [item] = model_trial.run_trial(database, MODELS, FakeProvider, limit=1)["items"]
    assert item["candidate_draft"] == {"error_type": "TimeoutError"}


@pytest.mark.parametrize("models", [
    {**MODELS, "draft": "gemini 3.8; rm"}, {**MODELS, "review": ""},
])
def test_model_names_are_validated(models):
    with pytest.raises(ValueError, match="model name"):
        model_trial.validate_models(models)


def test_limit_is_bounded(reviewed):
    session, _ = reviewed
    with pytest.raises(ValueError, match="limit"):
        model_trial.published_packages(Session(session.get_bind()), 16)

