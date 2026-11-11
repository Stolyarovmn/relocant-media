"""Версионированная редакционная конфигурация города без окружения и сетевых вызовов."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_CITY_ID = re.compile(r"[a-z][a-z0-9_]{1,63}\Z")
DEFAULT_CITIES_ROOT = Path(__file__).resolve().parents[3] / "cities"


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EditorialAudience(ProfileModel):
    name: str = Field(min_length=1, max_length=300)
    needs: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("needs")
    @classmethod
    def meaningful_needs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("Потребность аудитории не может быть пустой")
        return values


class EditorialGeography(ProfileModel):
    city_aliases: tuple[str, ...] = Field(min_length=1, max_length=30)
    country_aliases: tuple[str, ...] = Field(min_length=1, max_length=30)
    allow_country_scope: bool = True

    @field_validator("city_aliases", "country_aliases")
    @classmethod
    def meaningful_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("Географическое название не может быть пустым")
        return values


class EditorialRubric(ProfileModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=500)
    categories: tuple[str, ...] = Field(min_length=1, max_length=30)
    mode: Literal["single_story"] = "single_story"
    required_details: tuple[str, ...] = ()
    cadence: Literal["daily", "weekly"] = "weekly"
    target_per_period: int = Field(default=1, ge=0, le=14)
    priority: int = Field(default=50, ge=0, le=100)

    @field_validator("categories", "required_details")
    @classmethod
    def identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value) for value in values
        ):
            raise ValueError("Поля рубрики должны быть уникальными идентификаторами")
        return values


class CityEditorialProfile(ProfileModel):
    schema_version: Literal[1] = 1
    policy_version: str = Field(min_length=1, max_length=100)
    city_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    city_name: str = Field(min_length=1, max_length=100)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    country_name: str = Field(min_length=1, max_length=100)
    timezone: str = Field(min_length=1, max_length=100)
    # Платформа пока готовит русскоязычные материалы; страна от языка не зависит.
    content_language: Literal["ru"] = "ru"
    audience: EditorialAudience
    geography: EditorialGeography
    rubrics: tuple[EditorialRubric, ...] = Field(min_length=1, max_length=20)
    utility_min_local_relevance: int = Field(default=3, ge=3, le=4)
    utility_min_practical_value: int = Field(default=3, ge=3, le=4)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Неизвестный часовой пояс IANA") from exc
        return value

    @model_validator(mode="after")
    def unique_rubrics(self) -> Self:
        if len({rubric.id for rubric in self.rubrics}) != len(self.rubrics):
            raise ValueError("Идентификаторы рубрик должны быть уникальными")
        return self

    @property
    def sha256(self) -> str:
        serialized = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def rubric(self, rubric_id: str) -> EditorialRubric | None:
        return next((rubric for rubric in self.rubrics if rubric.id == rubric_id), None)


def load_city_profile(
    city: str, *, cities_root: Path | None = None,
) -> CityEditorialProfile:
    """Прочитать именно запрошенный профиль; неизвестный город никогда не заменяется."""
    if not _CITY_ID.fullmatch(city):
        raise ValueError("Недопустимый идентификатор города")
    root = (cities_root or DEFAULT_CITIES_ROOT).resolve()
    path = (root / city / "editorial.yaml").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Профиль находится вне каталога городов")
    if not path.is_file():
        raise ValueError(f"Редакционный профиль города {city} не настроен")
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Редакционный профиль превышает допустимый размер")
    profile = CityEditorialProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if profile.city_id != city:
        raise ValueError("Идентификатор профиля не совпадает с запрошенным городом")
    return profile
