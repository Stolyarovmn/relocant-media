"""#312: модель по ролям с откатом на GEMINI_MODEL."""
import json

import pytest

from relocant_media.ai.model_roles import RoleRoutedProvider, cache_model_id, role_models
from relocant_media.ops.server_cycle import ServerAI


def test_without_overrides_everything_stays_on_one_model():
    models = role_models({"GEMINI_MODEL": "lite"})
    assert set(models.values()) == {"lite"}
    # Ключ кэша не меняется, ранее подготовленные материалы не пересчитываются.
    assert cache_model_id(models) == "lite"


def test_overrides_split_draft_and_review():
    models = role_models({"GEMINI_MODEL": "lite", "GEMINI_MODEL_DRAFT": " strong ",
                          "GEMINI_MODEL_REVIEW": ""})
    assert models == {"default": "lite", "draft": "strong", "review": "lite"}
    assert cache_model_id(models) == "lite|draft=strong"


def test_default_model_is_required():
    with pytest.raises(ValueError):
        role_models({"GEMINI_MODEL_DRAFT": "strong"})


class Fake:
    def __init__(self, model, calls):
        self.model, self.calls = model, calls

    def generate_json(self, *, task, system_instruction, prompt, response_schema):
        self.calls.append((task, self.model))
        return "{}"


@pytest.mark.parametrize(("task", "model"), [
    ("rank", "lite"), ("claims", "lite"), ("verify", "lite"), ("batch_preselect", "lite"),
    ("draft", "strong"), ("repair_draft", "strong"), ("owner_revision", "strong"),
    ("independent_facts", "other"), ("independent_utility", "other"),
])
def test_tasks_reach_their_role_model(task, model):
    calls: list = []
    built: list = []

    def factory(name):
        built.append(name)
        return Fake(name, calls)

    routed = RoleRoutedProvider(
        {"default": "lite", "draft": "strong", "review": "other"}, factory)
    routed.generate_json(task=task, system_instruction="", prompt="", response_schema={})
    assert calls == [(task, model)]
    assert sorted(built) == ["lite", "other", "strong"]


def test_audit_records_the_model_that_actually_answered(tmp_path):
    calls: list = []
    routed = RoleRoutedProvider({"default": "lite", "draft": "strong", "review": "lite"},
                                lambda name: Fake(name, calls))
    audit = tmp_path / "ai-calls.jsonl"
    ai = ServerAI(routed, audit_path=audit, provider_name="gemini", model_id="lite")
    ai.generate_json(task="draft", system_instruction="", prompt="", response_schema={})
    ai.generate_json(task="rank", system_instruction="", prompt="", response_schema={})
    assert [json.loads(line)["model"] for line in audit.read_text().splitlines()] == [
        "strong", "lite"]
