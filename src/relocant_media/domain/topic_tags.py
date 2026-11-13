"""Стабильные хештеги русскоязычной редакции."""

import re

_TOPIC_TAGS = {
    "accusations": "#Город",
    "banking": "#Банки",
    "city": "#Город",
    "crime": "#Безопасность",
    "culture": "#Культура",
    "economy": "#Экономика",
    "events": "#События #Досуг",
    "health": "#Здоровье",
    "immigration": "#Документы",
    "jobs": "#Работа",
    "law": "#Право",
    "medicine": "#Медицина",
    "politics": "#Политика",
    "real_estate": "#Жильё",
    "schools": "#Школы",
    "sport": "#Спорт",
    "tax": "#Налоги",
    "transport": "#Транспорт",
    "utilities": "#Коммуналка",
}
_TAG_LINES = frozenset(_TOPIC_TAGS.values())
_TAG = re.compile(r"#[А-Яа-яЁё]+")


def topic_tags(category: str) -> str:
    return _TOPIC_TAGS.get(category, "#Город")


def is_topic_footer(line: str) -> bool:
    return line in _TAG_LINES


def topic_tag_spans(line: str) -> tuple[tuple[int, int], ...]:
    if not is_topic_footer(line):
        return ()
    return tuple(tag.span() for tag in _TAG.finditer(line))
