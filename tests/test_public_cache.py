import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.ai.provider import StructuredOutputError
from relocant_media.ai.public_cache import PublicPreparationCache

NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


class Provider:
    def __init__(self):
        self.calls = []
        self.response = '{"rationale":"Результат"}'

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def call(cache, task="rank", **changes):
    return cache.generate_json(**{"task": task, "system_instruction": "Правила",
        "prompt": "Публичная новость", "response_schema": {"type": "object"}, **changes})


def cache_at(provider, path, now=NOW, **kwargs):
    return PublicPreparationCache(provider, path, model_id="model-one",
                                  public_news_only=True, now=lambda: now, **kwargs)


@pytest.mark.parametrize("task", ["rank", "claims", "verify", "draft"])
def test_exact_preparation_cache_and_private_file_content(tmp_path, task):
    provider, audit = Provider(), []
    cache = cache_at(provider, tmp_path, audit=audit.append)
    assert call(cache, task) == call(cache, task)
    assert len(provider.calls) == 1
    assert [entry["cached"] for entry in audit] == [False, True]
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"key", "created_at", "response"}
    assert path.stem == payload["key"]
    assert "Публичная новость" not in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".preparation-*"))


@pytest.mark.parametrize("task", ["independent_facts", "independent_utility", "repair_draft",
                                  "unknown"])
def test_reviews_repairs_and_unknown_tasks_never_cached(tmp_path, task):
    provider = Provider()
    cache = cache_at(provider, tmp_path)
    call(cache, task)
    call(cache, task)
    assert len(provider.calls) == 2
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("change", ["prompt", "system_instruction", "response_schema", "task"])
def test_exact_input_changes_invalidate(tmp_path, change):
    provider = Provider()
    cache = cache_at(provider, tmp_path)
    call(cache)
    changes = {change: {"type": "string"} if change == "response_schema" else
               "draft" if change == "task" else "Изменено"}
    call(cache, **changes)
    assert len(provider.calls) == 2


def test_model_day_and_four_hour_ttl_invalidate(tmp_path):
    provider = Provider()
    call(cache_at(provider, tmp_path))
    call(cache_at(provider, tmp_path, NOW + timedelta(hours=3)))
    assert len(provider.calls) == 1
    call(cache_at(provider, tmp_path, NOW + timedelta(hours=4, seconds=1)))
    assert len(provider.calls) == 2
    other = PublicPreparationCache(provider, tmp_path, model_id="model-two",
        public_news_only=True, now=lambda: NOW)
    call(other)
    assert len(provider.calls) == 3
    # 22:00 UTC crosses local midnight in Belgrade in October, even within TTL.
    before = datetime(2026, 10, 20, 21, 59, tzinfo=UTC)
    call(cache_at(provider, tmp_path / "midnight", before))
    count = len(provider.calls)
    call(cache_at(provider, tmp_path / "midnight", before + timedelta(minutes=2)))
    assert len(provider.calls) == count + 1


@pytest.mark.parametrize("corrupt", ["not json", "[]", '{"key":"wrong"}',
    {"response": None}, {"response": ""}, {"created_at": "invalid"},
    {"created_at": (NOW + timedelta(minutes=1)).isoformat()}])
def test_corrupt_entry_is_cache_miss(tmp_path, corrupt):
    provider = Provider()
    cache = cache_at(provider, tmp_path)
    call(cache)
    path = next(tmp_path.glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(corrupt if isinstance(corrupt, str) else json.dumps({**payload, **corrupt}),
                    encoding="utf-8")
    call(cache)
    assert len(provider.calls) == 2


def test_provider_errors_never_cached_and_scope_requires_opt_in(tmp_path):
    provider = Provider()
    provider.response = RuntimeError("Ошибка")
    with pytest.raises(ValueError, match="public-news-only"):
        PublicPreparationCache(provider, tmp_path, model_id="model")
    cache = cache_at(provider, tmp_path)
    with pytest.raises(RuntimeError):
        call(cache)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("invalid", [None, "", "   ", 7])
def test_invalid_provider_output_not_cached(tmp_path, invalid):
    provider = Provider()
    provider.response = invalid
    cache = cache_at(provider, tmp_path)
    with pytest.raises(StructuredOutputError):
        call(cache)
    with pytest.raises(StructuredOutputError):
        call(cache)
    assert len(provider.calls) == 2
    assert not list(tmp_path.iterdir())


def test_schema_invalid_model_response_is_not_cached(tmp_path):
    from relocant_media.ai.models import RankingResult
    provider = Provider()
    cache = cache_at(provider, tmp_path)
    schema = RankingResult.model_json_schema()
    with pytest.raises(StructuredOutputError):
        call(cache, response_schema=schema)
    assert not list(tmp_path.glob("*.json"))
    provider.response = json.dumps({"should_process": False, "relevance": 0,
        "category": "city", "confidence": 1, "rationale": "Нет полезной новости"})
    assert call(cache, response_schema=schema) == provider.response
    assert call(cache, response_schema=schema) == provider.response
    assert len(provider.calls) == 2


def test_non_json_output_is_not_cached(tmp_path):
    provider = Provider()
    provider.response = "Неверный ответ вместо JSON"
    cache = cache_at(provider, tmp_path)
    with pytest.raises(StructuredOutputError):
        call(cache)
    assert not list(tmp_path.glob("*.json"))
