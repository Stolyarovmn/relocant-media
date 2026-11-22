"""Стоп повторной отдельной новости об отмене авиарейса; не решение о новизне.

Совпадение перевозчика, явно названной пары городов и дня требует проверенного
связанного обновления. Различие формулировок, числа рейсов или написания перевозчика
само по себе не даёт второй выпуск. Перевозчики и варианты их написания берутся из
реестра города `cities/<city>/carriers.json`, а не из кода. Разные перевозчики на
одном направлении в один день — независимые отмены. Нераспознанные перевозчики,
маршруты и даты этим правилом не классифицируются и идут обычным путём.
"""
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from relocant_media.domain.city_profile import DEFAULT_CITIES_ROOT
from relocant_media.domain.event_facts import event_dates

FLIGHT_REVIEW_REASON = "flight_linked_review"
_WINDOW = timedelta(hours=72)
_ROUTE = re.compile(
    r"\b(?:между|из)\s+([А-ЯЁ][а-яё-]{3,})\s+(?:и|в)\s+([А-ЯЁ][а-яё-]{3,})\b")
_WEEKDAYS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")


@lru_cache(maxsize=8)
def city_carriers(city: str) -> Mapping[str, re.Pattern[str]]:
    path = DEFAULT_CITIES_ROOT / city / "carriers.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("city_id") != city:
        raise ValueError(f"invalid carrier registry for {city}")
    return {
        carrier: re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
            + r")(?:а|и|ы|у|ой|е)?(?!\w)", re.I)
        for carrier, aliases in data["carriers"].items()
    }


def _carrier(title: str, carriers: Mapping[str, re.Pattern[str]]) -> str | None:
    found = {carrier for carrier, pattern in carriers.items() if pattern.search(title)}
    return found.pop() if len(found) == 1 else None


def _city(value: str) -> str:
    # Только падежи односоставных русских названий, без свободной геокодировки.
    return re.sub(r"(?:ом|ем|а|е|у|ь)$", "", value.casefold())


def _notice(
    title: str, body: str, reference: datetime, carriers: Mapping[str, re.Pattern[str]],
) -> tuple[object, ...] | None:
    if reference.tzinfo is None or max(len(title), len(body)) > 8000:
        return None
    carrier = _carrier(title, carriers)
    if carrier is None:
        return None
    if not (re.search(r"\bотмен\w*", title, re.I)
            and re.search(r"\b(?:авиа)?рейс\w*", title, re.I)):
        return None
    if re.search(r"\bне\s+(?:будет\s+)?отмен\w*", title, re.I):
        return None
    text = title + "\n" + body
    routes = {tuple(sorted((_city(a), _city(b)))) for a, b in _ROUTE.findall(text)}
    if len(routes) != 1 or len(set(next(iter(routes)))) != 2:
        return None
    local = reference.astimezone(ZoneInfo("Europe/Belgrade"))
    dates = event_dates(text, local.year)
    if not dates:
        if re.search(r"кажд\w*|еженедел\w*", text, re.I):
            return None
        weekdays = {i for i, stem in enumerate(_WEEKDAYS)
                    if re.search(r"\b(?:в|на)\s+" + stem + r"\w*\b", text, re.I)}
        if len(weekdays) != 1:
            return None
        day = local.date() + timedelta(days=(weekdays.pop() - local.weekday()) % 7)
        dates = frozenset({day.isoformat()})
    if len(dates) != 1:
        return None
    try:
        datetime.fromisoformat(next(iter(dates)))
    except ValueError:
        return None
    return (carrier, next(iter(routes)), dates)


def find_related_flight_cancellation(
    title: str, body: str,
    publications: Iterable[tuple[int, str, str, datetime | None]], *, now: datetime,
    city: str = "belgrade",
) -> int | None:
    """Возвращает прежний пост для удержания, не для автоматического отказа."""
    carriers = city_carriers(city)
    notice = _notice(title, body, now, carriers)
    if notice is None:
        return None
    for post_id, old_title, old_body, published in publications:
        if (published is not None and published.tzinfo is not None
                and timedelta() <= now - published <= _WINDOW
                and notice == _notice(old_title, old_body, published, carriers)):
            return post_id
    return None
