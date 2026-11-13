"""Стабильные хештеги русскоязычной редакции."""

import re
from importlib.resources import files
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _TopicTagRules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    fallback: Annotated[str, Field(pattern=r"^#[А-Яа-яЁё]+$")]
    categories: dict[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")],
        Annotated[str, Field(pattern=r"^#[А-Яа-яЁё]+$")],
    ] = Field(min_length=1)


_RULES_V1 = _TopicTagRules.model_validate(yaml.safe_load(
    files(__package__).joinpath("publication_tags_v1.yaml").read_text(encoding="utf-8"),
))
_LEGACY_EVENT_TAGS = "#События #Досуг"
_TAG_LINES = frozenset((*_RULES_V1.categories.values(), _RULES_V1.fallback, _LEGACY_EVENT_TAGS))
_TAG = re.compile(r"#[А-Яа-яЁё]+")
_DRAFT_TAG = r"(?<!\w)#+(?=\w*[^\W\d_])\w+(?:@[a-z0-9_]{3,32})?"
_TAG_ONLY_LINE = re.compile(rf"{_DRAFT_TAG}(?:\s+{_DRAFT_TAG})*", re.IGNORECASE)
_DRAFT_LINK_OR_TAG = re.compile(
    rf"(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>«»]+|(?P<tag>{_DRAFT_TAG})", re.IGNORECASE,
)


def topic_tags(category: str, *, presentation_version: int) -> str:
    if category == "events" and presentation_version == 2:
        return _LEGACY_EVENT_TAGS
    return _RULES_V1.categories.get(category, _RULES_V1.fallback)


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
