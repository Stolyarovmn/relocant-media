"""Стабильные хештеги русскоязычной редакции."""

import re
from importlib.resources import files
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

_Tag = Annotated[str, Field(pattern=r"^#[А-Яа-яЁё]+$")]
_Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class _TopicTagRules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fallback: _Tag
    categories: dict[_Key, _Tag] = Field(min_length=1)
    rubric_overrides: dict[_Key, _Tag] = Field(default_factory=dict)
    legacy_lines: list[Annotated[str, Field(pattern=r"^#[А-Яа-яЁё]+(?: #[А-Яа-яЁё]+)*$")]] = []


def _load(name: str) -> _TopicTagRules:
    return _TopicTagRules.model_validate(yaml.safe_load(
        files(__package__).joinpath(name).read_text(encoding="utf-8"),
    ))


_RULES_V1 = _load("publication_tags_v1.yaml")
_RULES_V2 = _load("publication_tags_v2.yaml")
TOPIC_CATEGORIES = frozenset(_RULES_V1.categories)
# Первая версия с правилами v2; более ранние пакеты проверяются по v1.
TAGS_V2_PRESENTATION = 6
_LEGACY_EVENT_TAGS = "#События #Досуг"
_TAG_LINES = frozenset((
    *(tag for rules in (_RULES_V1, _RULES_V2) for tag in (
        *rules.categories.values(), *rules.rubric_overrides.values(), rules.fallback,
        *rules.legacy_lines)),
    _LEGACY_EVENT_TAGS,
))
_TAG = re.compile(r"#[А-Яа-яЁё]+")
_DRAFT_TAG = r"(?<!\w)#+(?=\w*[^\W\d_])\w+(?:@[a-z0-9_]{3,32})?"
_TAG_ONLY_LINE = re.compile(rf"{_DRAFT_TAG}(?:\s+{_DRAFT_TAG})*", re.IGNORECASE)
_DRAFT_LINK_OR_TAG = re.compile(
    rf"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>«»]+|(?P<tag>{_DRAFT_TAG})", re.IGNORECASE,
)


def topic_tags(category: str, *, presentation_version: int, rubric_id: str = "") -> str:
    """Один публичный хештег. Рубрика уточняет только городскую или неизвестную категорию."""
    if category == "events" and presentation_version == 2:
        return _LEGACY_EVENT_TAGS
    if presentation_version < TAGS_V2_PRESENTATION:
        return _RULES_V1.categories.get(category, _RULES_V1.fallback)
    tag = _RULES_V2.categories.get(category)
    if tag is not None and category != "city":
        return tag
    return _RULES_V2.rubric_overrides.get(rubric_id, tag or _RULES_V2.fallback)


def is_draft_tag_line(line: str) -> bool:
    return _TAG_ONLY_LINE.fullmatch(line.strip()) is not None


def strip_draft_tags(text: str) -> str:
    """Сохранить слова меток внутри текста и не менять ссылки."""
    return _DRAFT_LINK_OR_TAG.sub(
        lambda match: match[0].lstrip("#") if match["tag"] else match[0], text,
    )


def is_topic_footer(line: str) -> bool:
    return line in _TAG_LINES


def topic_tag_spans(line: str) -> tuple[tuple[int, int], ...]:
    if not is_topic_footer(line):
        return ()
    return tuple(tag.span() for tag in _TAG.finditer(line))
