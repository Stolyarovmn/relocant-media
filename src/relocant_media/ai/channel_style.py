"""Правила подачи для подготовки текста и независимой проверки."""

from datetime import datetime
from importlib.resources import files
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from relocant_media.ai.models import WRITING_MATRIX_VERSION_PATTERN
from relocant_media.domain.city_profile import CityEditorialProfile


class _WritingFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: tuple[str, ...] = Field(min_length=1)
    words: tuple[int, int]
    structure: str = Field(min_length=1)
    tone: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_range(self) -> "_WritingFormat":
        if not 0 < self.words[0] <= self.words[1]:
            raise ValueError("Неверный диапазон слов")
        return self


class _WritingMatrix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(pattern=WRITING_MATRIX_VERSION_PATTERN)
    common: str
    voice: str
    context: str
    weather: str
    guide: str
    procedural_guidance: str
    formats: dict[str, _WritingFormat]

    @model_validator(mode="after")
    def distinct_categories(self) -> "_WritingMatrix":
        categories = [category for rule in self.formats.values() for category in rule.categories]
        if "city" not in self.formats or len(set(categories)) != len(categories):
            raise ValueError("Категории матрицы должны быть уникальны, нужен формат city")
        return self


_MATRIX = _WritingMatrix.model_validate(yaml.safe_load(
    files(__package__).joinpath("writing_matrix.yaml").read_text(encoding="utf-8"),
))
_BY_CATEGORY = {category: rule for rule in _MATRIX.formats.values() for category in rule.categories}
CHANNEL_STYLE_VERSION = _MATRIX.version
PROCEDURAL_GUIDANCE_POLICY = _MATRIX.procedural_guidance
CHANNEL_DRAFT_STYLE = (
    f"\n{_MATRIX.common}\n{_MATRIX.voice}\n{_MATRIX.context}\n{_MATRIX.weather}\n"
    f"{PROCEDURAL_GUIDANCE_POLICY}\n"
)


def word_range(category: str) -> tuple[int, int]:
    """Matrix word range of the category; unknown categories use the city format."""
    return _BY_CATEGORY.get(category, _MATRIX.formats["city"]).words


def writing_instruction(category: str, *, evergreen: bool = False) -> str:
    if evergreen:
        return f"\n{_MATRIX.guide}\n"
    rule = _BY_CATEGORY.get(category, _MATRIX.formats["city"])
    low, high = word_range(category)
    return f"\nДля категории {category}: {low}–{high} слов. {rule.structure} {rule.tone}\n"


def writing_context(profile: CityEditorialProfile | None, now: datetime) -> dict[str, str]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Время подготовки должно содержать часовой пояс")
    zone = profile.timezone if profile else "Europe/Belgrade"
    local = now.astimezone(ZoneInfo(zone))
    return {
        "timezone": zone,
        "prepared_at_local": local.isoformat(timespec="minutes"),
        "day_of_week": ("пн", "вт", "ср", "чт", "пт", "сб", "вс")[local.weekday()],
        "daypart": ("ночь", "утро", "день", "вечер")[local.hour // 6],
    }


def writing_brief(profile: CityEditorialProfile, now: datetime) -> dict[str, Any]:
    from relocant_media.ai.draft_coverage import FACTS_COVERAGE_INSTRUCTION, DraftFactsCheck

    return {**_MATRIX.model_dump(mode="json"), "calendar": writing_context(profile, now),
            "facts_review_contract": {
                "placement": "package.reviews[role=facts].facts_check",
                "instruction": FACTS_COVERAGE_INSTRUCTION,
                "schema": DraftFactsCheck.model_json_schema(),
            }}
