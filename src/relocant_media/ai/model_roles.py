"""Модель по ролям (#312): черновик и ревью можно отдать другой модели, чем отбор.

Без переменных ``GEMINI_MODEL_DRAFT`` и ``GEMINI_MODEL_REVIEW`` всё работает на
``GEMINI_MODEL``, как раньше, включая ключ кэша. Переменные — настройка владельца;
код сам модель не меняет.
"""
from collections.abc import Callable, Mapping
from typing import Any

from relocant_media.ai.provider import StructuredAIProvider

DRAFT_TASKS = frozenset({"draft", "repair_draft", "owner_revision"})
REVIEW_TASKS = frozenset({"independent_facts", "independent_utility"})
ROLE_VARIABLES = {"draft": "GEMINI_MODEL_DRAFT", "review": "GEMINI_MODEL_REVIEW"}


def role_for(task: str) -> str:
    if task in DRAFT_TASKS:
        return "draft"
    if task in REVIEW_TASKS:
        return "review"
    return "default"


def role_models(env: Mapping[str, str]) -> dict[str, str]:
    default = env.get("GEMINI_MODEL", "").strip()
    if not default:
        raise ValueError("GEMINI_MODEL is required")
    models = {"default": default}
    for role, name in ROLE_VARIABLES.items():
        models[role] = env.get(name, "").strip() or default
    return models


def cache_model_id(models: Mapping[str, str]) -> str:
    """Prior cache keys stay valid while no role override is set."""
    extra = "".join(f"|{role}={models[role]}" for role in ROLE_VARIABLES
                    if models[role] != models["default"])
    return models["default"] + extra


class RoleRoutedProvider:
    """Route each structured task to its role's provider; one provider per distinct model."""

    def __init__(self, models: Mapping[str, str],
                 factory: Callable[[str], StructuredAIProvider]) -> None:
        self.models = dict(models)
        built: dict[str, StructuredAIProvider] = {}
        for model in dict.fromkeys(self.models.values()):
            built[model] = factory(model)
        self._providers = built

    def model_for(self, task: str) -> str:
        return self.models[role_for(task)]

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        return self._providers[self.model_for(task)].generate_json(
            task=task, system_instruction=system_instruction, prompt=prompt,
            response_schema=response_schema)
