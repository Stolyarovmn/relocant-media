"""Замер соблюдения матрицы подачи (#313): только подсказки редактору, не блок.

Считает слова основного текста против диапазона категории, длинные абзацы и
канцелярит из запретов матрицы. Списки и блоки с подписями («Где:», «Когда:»)
из полей оформления не считаются длинными абзацами: это допустимая плотная подача.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

LONG_PARAGRAPH_WORDS = 45
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_LIST_LINE = re.compile(r"^\s*(?:[-–—•·*]|\d+[.)])\s+")
# Только поля оформления из матрицы и близкие к ним; вводная «Редакция уточняет:»
# подписью не считается.
FORMAT_LABELS = ("Где", "Когда", "Билеты", "Объезд", "Адрес", "Вход", "Цена", "Время",
                 "Маршрут")
_LABEL_LINE = re.compile(
    r"^\s*(?:[^\w\s]+\s*)?(?:" + "|".join(FORMAT_LABELS) + r"):\s")
# Запреты из раздела common матрицы и её пример «будет приостановлена подача воды».
# «данные» и «по данным» — обычные слова, поэтому в список не входят.
BUREAUCRATESE: dict[str, re.Pattern[str]] = {
    "в связи с": re.compile(r"\bв\s+связи\s+с(?:о)?\b", re.IGNORECASE),
    "осуществляется": re.compile(r"\bосуществля\w*", re.IGNORECASE),
    "согласно информации": re.compile(r"\bсогласно\s+информации\b", re.IGNORECASE),
    "данный": re.compile(r"\bданн(?:ый|ая|ое|ого|ой|ом|ую)\b", re.IGNORECASE),
    "в целях": re.compile(r"\bв\s+целях\b", re.IGNORECASE),
    "является": re.compile(r"\bявля(?:ется|ются)\b", re.IGNORECASE),
    "будет приостановлен": re.compile(r"\bбуд(?:ет|ут)\s+приостановлен\w*", re.IGNORECASE),
}


def count_words(text: str) -> int:
    return len(_WORD.findall(_URL.sub(" ", text)))


def _paragraphs(body: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n", body.strip()) if part.strip()]


def _structured(paragraph: str) -> bool:
    lines = [line for line in paragraph.splitlines() if line.strip()]
    return bool(lines) and all(_LIST_LINE.match(line) or _LABEL_LINE.match(line)
                               for line in lines)


@dataclass(frozen=True, slots=True)
class StyleFlags:
    words: int
    word_range: tuple[int, int]
    over_range: bool
    under_range: bool
    long_paragraphs: int
    bureaucratese: dict[str, int] = field(default_factory=dict)


def lint(body: str, word_range: tuple[int, int]) -> StyleFlags:
    """Flags for one post body. Short accurate posts are allowed by the matrix:
    ``under_range`` is a hint to look, not a defect."""
    low, high = word_range
    words = count_words(body)
    long_paragraphs = sum(
        1 for paragraph in _paragraphs(body)
        if not _structured(paragraph) and count_words(paragraph) > LONG_PARAGRAPH_WORDS)
    text = _URL.sub(" ", body)
    found = {name: len(pattern.findall(text)) for name, pattern in BUREAUCRATESE.items()}
    return StyleFlags(words=words, word_range=word_range, over_range=words > high,
                      under_range=words < low, long_paragraphs=long_paragraphs,
                      bureaucratese={name: count for name, count in found.items() if count})
