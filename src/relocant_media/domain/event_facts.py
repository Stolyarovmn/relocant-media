"""Ограниченные проверяемые признаки; отсутствие признака не означает совпадение.

Общие проверки чисел/дат применимы к любому городу. Адаптер транспортных
объявлений понимает сербскую латиницу; новые языки требуют отдельных фикстур.
Здесь намеренно нет названий городов, улиц или номеров конкретных маршрутов.
"""

from __future__ import annotations

import re
import unicodedata

_MONTHS = (
    r"jan\w*|январ\w*",
    r"feb\w*|феврал\w*",
    r"mar\w*|март\w*",
    r"apr\w*|апрел\w*",
    r"maj\w*|may|ма[йя]",
    r"jun\w*|июн\w*",
    r"jul\w*|июл\w*",
    r"avg\w*|aug\w*|август\w*",
    r"sep\w*|сентябр\w*",
    r"okt\w*|oct\w*|октябр\w*",
    r"nov\w*|ноябр\w*",
    r"dec\w*|декабр\w*",
)
_MONTH_PATTERN = "|".join(_MONTHS)
_NAMED_DATE = re.compile(
    rf"\b(?:(\d{{1,2}})\.?\s*(?:do|до|to|[-–])\s*)?"
    rf"(\d{{1,2}})\.?\s+({_MONTH_PATTERN})(?:\s+(\d{{4}})\b)?",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_CLOCK = re.compile(
    r"\b(?:od|from|с)\s+(\d{1,2})(?:[.:](\d{2}))?\s*"
    r"(?:do|to|до|[-–])\s*(\d{1,2})(?:[.:](\d{2}))?"
    r"\s*(?:čas\w*|cas\w*|sat\w*|hour\w*|час\w*|ч\b)",
    re.IGNORECASE,
)
_MATERIAL_WORDS = re.compile(
    r"\b(?:ne|neće|nece|nije|nisu|nema|bez|not|no|without|не|нет|без|"
    r"otkaz\w*|cancel\w*|отмен\w*|jednosmer\w*|u?dvosmer\w*|"
    r"north\w*|south\w*|east\w*|west\w*|sever\w*|jug\w*|istok\w*|zapad\w*|"
    r"север\w*|юг\w*|юж\w*|восток\w*|запад\w*)\b",
    re.IGNORECASE,
)


def _plain(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def event_dates(value: str, year: int) -> frozenset[str]:
    """Сравнивает календарные даты, сохраняя месяц, год и границы периода."""
    dates = set()
    for match in _NAMED_DATE.finditer(_plain(value)):
        first, last, month_name, explicit_year = match.groups()
        month = next(i for i, pattern in enumerate(_MONTHS, 1) if re.fullmatch(pattern, month_name))
        for day in (first, last):
            if day is not None:
                dates.add(f"{int(explicit_year or year):04}-{month:02}-{int(day):02}")
    for match in _NUMERIC_DATE.finditer(value):
        day, month_number, explicit_year = map(int, match.groups())
        dates.add(f"{explicit_year:04}-{month_number:02}-{day:02}")
    return frozenset(dates)


def clock_ranges(value: str) -> frozenset[tuple[int, int]]:
    return frozenset(
        (int(start) * 60 + int(start_min or 0), int(end) * 60 + int(end_min or 0))
        for start, start_min, end, end_min in _CLOCK.findall(value)
    )


def material_words(value: str) -> frozenset[str]:
    text = _plain(value)
    actions = re.findall(r"\b(ukid|izmešt|izmest|uspostav)\w*\b", text)
    return frozenset(
        "dvosmer" if word.startswith(("udvosmer", "dvosmer")) else word
        for word in _MATERIAL_WORDS.findall(text) + actions
    )


def location_anchors(value: str) -> frozenset[str]:
    # Только явно обозначенные имена после местного предлога. Вольное
    # распознавание географии без словаря создало бы ложную уверенность.
    return frozenset(
        _plain(name)
        for name in re.findall(
            r"\b(?:[Вв]|[Уу]|[Ii]n|[Aa]t|[Uu])\s+"
            r"([A-ZА-ЯЁČĆŠŽĐ][a-zа-яёčćšžđ]+(?:\s+[A-ZА-ЯЁČĆŠŽĐ][a-zа-яёčćšžđ]+)*)",
            value,
        )
    )


def numeric_facts(value: str, year: int) -> frozenset[str]:
    """Различает числа и идентификаторы; даты/часы нормализует отдельно."""
    text = _NUMERIC_DATE.sub(" ", _NAMED_DATE.sub(" ", _CLOCK.sub(" ", _plain(value))))
    numbers = {
        str(int(number)) + suffix for number, suffix in re.findall(r"\b(\d+)([a-zа-я]*)\b", text)
    }
    # Порядок счёта/времени существенен: 103:73 не равно 73:103.
    numbers.update(
        f"pair:{int(first)}:{separator}:{int(last)}"
        for first, separator, last in re.findall(r"\b(\d+)([:.,])(\d+)\b", text)
    )
    return frozenset(
        numbers
        | set(event_dates(value, year))
        | {f"clock:{start}:{end}" for start, end in clock_ranges(value)}
    )


def conflicting_facts(left: str, right: str, left_year: int, right_year: int) -> bool:
    """При fuzzy-сопоставлении новые/пропавшие числа тоже требуют нового ревью."""
    locations_left, locations_right = location_anchors(left), location_anchors(right)
    return (
        numeric_facts(left, left_year) != numeric_facts(right, right_year)
        or material_words(left) != material_words(right)
        or bool(locations_left and locations_right and locations_left != locations_right)
    )


def transport_routes(value: str) -> frozenset[str]:
    # Поддерживает списки после «linije», «linija broj» и двоеточия.
    routes: set[str] = set()
    pattern = re.compile(
        r"\blinij\w*(?:\s+(?:broj|br))?\s+(\d{1,3}[a-z]?(?:\s*[,i]\s*\d{1,3}[a-z]?)*)"
        r"|:\s*(\d{1,3}[a-z]?(?:\s*[,i]\s*\d{1,3}[a-z]?){2,})",
        re.IGNORECASE,
    )
    for match in pattern.finditer(value):
        routes.update(re.findall(r"\d{1,3}[a-z]?", match.group(1) or match.group(2), re.I))
    return frozenset(route.upper() for route in routes)


def _street_stem(value: str) -> str:
    return re.sub(r"(?:oj|om|a|e|i|u)$", "", value.casefold())


def street_anchors(value: str) -> tuple[str, ...]:
    # Ограниченное распознавание склоняемых сербских названий улиц.
    matches = list(re.finditer(r"\bulic\w*\s+([A-ZČĆŠŽĐ][\w-]+)", value))
    matches += list(re.finditer(r"\b([A-ZČĆŠŽĐ][\w-]+)\s+ulic\w*\b", value))
    names = [match.group(1) for match in sorted(matches, key=lambda match: match.start())]
    return tuple(
        dict.fromkeys(
            _street_stem(name) for name in names if not name.casefold().startswith("bulevar")
        )
    )


def terminal_anchor(value: str) -> str | None:
    match = re.search(r"\bdo\s+(?:SRC-a|terminusa)\s+[„\"“]([^”\"“]+)", value, re.I)
    if not match:
        return None
    # «район /SRC название/» и «SRC название» обозначают один терминус.
    name = match.group(1).strip(" /")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    words = re.findall(r"\w+", name.casefold())
    if words and words[0] == "src":
        words = words[1:]
    return " ".join(words) if len(words) >= 3 else None


def enhanced_service_anchor(value: str) -> tuple[str, str] | None:
    if not re.search(r"\b(?:pojačan\w*|povećan\w*)\b", value, re.I):
        return None
    match = re.search(r"\blinij\w*\s+(\d{1,3}[a-z])\s*\(([^)]+)\)", value, re.I)
    if not match or not re.search(r"\buspostav\w*\b", value, re.I):
        return None
    return match.group(1).upper(), " ".join(re.findall(r"\w+", match.group(2).casefold()))
