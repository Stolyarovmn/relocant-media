"""#312: проба моделей читает снимок и ничего не пишет и не отправляет."""
import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from test_channel_delivery import reviewed as reviewed_fixture

from relocant_media.ai.models import EditorialOutcome, PublicationStatus, RankingResult
from relocant_media.ops import model_trial
from relocant_media.storage.models import EditorialRun, Post, Submission

reviewed = reviewed_fixture
CURRENT = {"default": "gemini-3.5-flash-lite", "draft": "gemini-3.5-flash-lite",
           "review": "gemini-3.5-flash-lite"}
MODELS = {"default": "gemini-3.5-flash-lite", "draft": "gemini-3.8-flash",
          "review": "gemini-3.6-flash"}
BUNDLES = {"current": CURRENT, "candidate": MODELS}


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
        return '{"current": true}' if self.model == "gemini-3.5-flash-lite" else "{}"


SKIPPED = EditorialOutcome(status=PublicationStatus.SKIPPED, reason="not_local",
    ranking=RankingResult(should_process=False, relevance=0, category="city",
                          confidence=1, rationale="x"), confidence=1)


@pytest.fixture
def fakes(monkeypatch):
    calls = []
    state = {"outcome": SKIPPED}

    class Pipeline:
        def __init__(self, provider, city_profile=None):
            self.provider = provider

        def run(self, *, city, documents, now):
            self.provider.generate_json(task="draft", system_instruction="", prompt="",
                                        response_schema={})
            calls.append(("draft", now))
            return state["outcome"]

    def review(package, provider, live, *, now, audit):
        assert all(item.verdict == "hold" for item in package.reviews)
        assert live() == tuple(package.event.documents)
        for role in ("facts", "utility"):
            answer = provider.generate_json(task=f"independent_{role}", system_instruction="",
                                            prompt="prompt", response_schema={})
            verdict = "approve" if answer == '{"current": true}' else "hold"
            audit({"role": role, "verdict": verdict, "computed_score": 40,
                   "flags": ["Дата не указана"], "reason": "Нет даты в тексте источника"})
        calls.append(("review", package.outcome.draft.title))

    monkeypatch.setattr(model_trial, "EditorialPipeline", Pipeline)
    monkeypatch.setattr(model_trial, "reviewed_package", review)
    return calls, state


def test_trial_pairs_bundles_on_same_post_without_writing_state(reviewed, fakes):
    calls, _ = fakes
    session, run = reviewed
    _sent(session, run)
    database = session.get_bind().url.database
    before = hashlib.sha256(open(database, "rb").read()).hexdigest()
    result = model_trial.run_trial(database, BUNDLES, FakeProvider, limit=5)
    assert hashlib.sha256(open(database, "rb").read()).hexdigest() == before
    [item] = result["items"]
    # Ревью опубликованного текста: сравнение ревьюеров отдельно.
    assert item["current"]["published"]["double_approval"] is True
    assert item["candidate"]["published"]["double_approval"] is False
    assert item["candidate"]["published"]["reviews"] == {
        "facts": {"verdict": "hold", "flags": 1}, "utility": {"verdict": "hold", "flags": 1}}
    # Черновик не готов: полная связка не одобрена даже при одобренном старом тексте.
    assert item["current"]["full"]["double_approval"] is False
    assert item["candidate"]["calls"] == 3 and item["candidate"]["input_chars"] == 12
    assert item["facts_verdicts_differ"] is True
    # Модель видит время исходной публикации, а не текущее; обе связки — один материал.
    assert {when for kind, when in calls if kind == "draft"} == {
        datetime(2026, 11, 21, 10, tzinfo=UTC)}
    assert len(calls) == 4
    summary = result["summary"]
    assert summary["bundles"]["current"]["published_double_approval"] == 1
    assert summary["bundles"]["current"]["full_double_approval"] == 0
    assert summary["bundles"]["candidate"]["published_facts_flags"] == 1
    assert summary["bundles"]["candidate"]["by_role_and_model"] == {
        "draft:gemini-3.8-flash": {"calls": 1, "errors": 0, "input_chars": 0,
                                   "output_chars": 2},
        "review:gemini-3.6-flash": {"calls": 2, "errors": 0, "input_chars": 12,
                                    "output_chars": 4}}
    assert summary["facts_verdicts_differ"] == 1
    text = model_trial.markdown(result)
    assert "| candidate | default=gemini-3.5-flash-lite, draft=gemini-3.8-flash" in text
    assert "| candidate | review:gemini-3.6-flash | 2 | 0 | 12 | 4 |" in text


def test_full_bundle_reviews_its_own_new_draft(reviewed, fakes):
    calls, state = fakes
    session, run = reviewed
    _sent(session, run)
    stored = model_trial.EditorialPackage.model_validate_json(
        json.dumps(run.outcome["_package"]))
    new_draft = stored.outcome.draft.model_copy(update={"title": "Новый заголовок связки"})
    state["outcome"] = stored.outcome.model_copy(update={
        "status": PublicationStatus.READY, "draft": new_draft})
    result = model_trial.run_trial(session.get_bind().url.database, BUNDLES, FakeProvider,
                                   limit=1)
    [item] = result["items"]
    assert item["current"]["draft_status"] == "ready"
    assert item["current"]["full"]["double_approval"] is True
    assert item["candidate"]["full"]["double_approval"] is False
    assert item["candidate"]["full"]["reviews"]["facts"] == {"verdict": "hold", "flags": 1}
    reviewed_titles = [title for kind, title in calls if kind == "review"]
    assert reviewed_titles.count("Новый заголовок связки") == 2
    assert result["summary"]["bundles"]["current"]["full_double_approval"] == 1
    assert result["summary"]["bundles"]["candidate"]["full_facts_flags"] == 1


def test_uploaded_file_holds_only_anonymized_summary(reviewed, fakes, tmp_path):
    session, run = reviewed
    _sent(session, run)
    package = run.outcome["_package"]
    result = model_trial.run_trial(session.get_bind().url.database, BUNDLES, FakeProvider,
                                   limit=1)
    output, summary = tmp_path / "model-trial.json", tmp_path / "summary.md"
    model_trial.write_outputs(result, output, str(summary))
    uploaded = json.loads(output.read_text(encoding="utf-8"))
    assert set(uploaded) == {"version", "generated_at", "models", "summary"}
    for text in (output.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")):
        for secret in (package["outcome"]["draft"]["title"], package["outcome"]["draft"]["body"],
                       package["evidence"]["title"], "Нет даты в тексте источника",
                       "Дата не указана", "prompt", '"raw_item_id"', '"category"'):
            assert secret not in text


def test_workflow_uploads_the_file_written_by_the_trial():
    workflow = open(".github/workflows/model-trial.yml", encoding="utf-8").read()
    assert '--output "$RUNNER_TEMP/model-trial.json"' in workflow
    assert "path: ${{ runner.temp }}/model-trial.json" in workflow


def test_reader_material_is_excluded_whole(reviewed, fakes):
    session, run = reviewed
    database = session.get_bind().url.database
    assert model_trial.run_trial(database, BUNDLES, FakeProvider, limit=5)["items"] == []
    _sent(session, run, _reader_submission_id=5)
    assert model_trial.run_trial(database, BUNDLES, FakeProvider, limit=5)["items"] == []
    # Без пометки в прогоне, но материал связан с заявкой читателя.
    stored = session.get(EditorialRun, run.raw_item_id)
    stored.outcome = {k: v for k, v in stored.outcome.items() if k != "_reader_submission_id"}
    session.add(Submission(kind="reader_tip", payload={"raw_item_ids": [run.raw_item_id]}))
    session.commit()
    assert model_trial.run_trial(database, BUNDLES, FakeProvider, limit=5)["items"] == []
    assert fakes[0] == []


def test_reader_linked_event_document_excludes_package(reviewed, fakes, monkeypatch):
    session, run = reviewed
    _sent(session, run)
    package = model_trial.EditorialPackage.model_validate_json(
        json.dumps(run.outcome["_package"]))
    [document] = package.event.documents
    monkeypatch.setattr(model_trial, "reader_linked_raw_ids",
                        lambda session: {document.raw_item_id})
    database = session.get_bind().url.database
    assert model_trial.run_trial(database, BUNDLES, FakeProvider, limit=5)["items"] == []
    assert fakes[0] == []


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
    result = model_trial.run_trial(database, BUNDLES, FakeProvider, limit=1)
    [item] = result["items"]
    assert item["candidate"]["draft_status"] == "error"
    assert item["candidate"]["draft_error_type"] == "TimeoutError"
    assert item["candidate"]["full"]["double_approval"] is False
    assert result["summary"]["bundles"]["candidate"]["error_types"] == {"TimeoutError": 1}
    assert "secret response body" not in json.dumps(result)


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



def test_trial_waits_for_running_cycle_without_touching_state():
    states = iter([{"status": "busy"}, {"status": "busy"}, {"status": "ready", "snapshot": {}}])
    pauses, validated = [], []
    state = model_trial.wait_ready(lambda: next(states), validated.append, attempts=5,
                                   pause=7, sleep=pauses.append)
    assert state["status"] == "ready" and validated == [state] and pauses == [7, 7]


def test_trial_gives_up_when_state_stays_busy():
    pauses = []
    with pytest.raises(TimeoutError):
        model_trial.wait_ready(lambda: {"status": "busy"}, lambda state: None, attempts=3,
                               pause=1, sleep=pauses.append)
    assert pauses == [1, 1]


def test_trial_rejects_state_that_is_not_ready():
    def validate(state):
        raise ValueError("canonical state not ready")

    with pytest.raises(ValueError):
        model_trial.wait_ready(lambda: {"status": "failed"}, validate, attempts=3,
                               sleep=lambda pause: None)
