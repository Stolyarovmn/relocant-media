"""Узкий стоп повторной отдельной отмены рейса; не решение о новизне.

Совпадение перевозчика, пары городов и дня требует проверенного связанного
обновления. Различие формулировок/числа рейсов само по себе не даёт второй выпуск.
Другие перевозчики и нераспознанные маршруты этим правилом не классифицируются.
"""
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from relocant_media.domain.event_facts import event_dates

FLIGHT_REVIEW_REASON = "flight_linked_review"
_WINDOW = timedelta(hours=72)
_AIRLINE = re.compile(r"\b(?:air\s+serbia|er\s+srbija|[эе]р\s+серби[яи])\b", re.I)
_ROUTE = re.compile(
    r"\b(?:между|из)\s+([А-ЯЁ][а-яё-]{3,})\s+(?:и|в)\s+([А-ЯЁ][а-яё-]{3,})\b")
_WEEKDAYS = ("понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень")


def _city(value: str) -> str:
    # Только падежи односоставных русских названий, без свободной геокодировки.
    return re.sub(r"(?:ом|ем|а|е|у|ь)$", "", value.casefold())


def _notice(title: str, body: str, reference: datetime) -> tuple[object, ...] | None:
    if reference.tzinfo is None or max(len(title), len(body)) > 8000:
        return None
    if not (_AIRLINE.search(title) and re.search(r"\bотмен\w*", title, re.I)
            and re.search(r"\bрейс\w*", title, re.I)):
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
    return ("air_serbia", next(iter(routes)), dates)


def flight_cancellation_key(
    title: str, body: str, *, published_at: datetime | None,
) -> tuple[object, ...] | None:
    """Ключ одного события для сводки, с днём недели от времени самого поста.

    Отсутствующие/неоднозначные признаки не объединяются. Это не меняет отдельные
    посты, их исходники, оценки новизны, опубликованные квитанции или правила отправки.
    """
    return None if published_at is None else _notice(title, body, published_at)

def find_related_flight_cancellation(
    title: str, body: str,
    publications: Iterable[tuple[int, str, str, datetime | None]], *, now: datetime,
) -> int | None:
    """Возвращает прежний пост для удержания, не для автоматического отказа."""
    notice = _notice(title, body, now)
    if notice is None:
        return None
    for post_id, old_title, old_body, published in publications:
        if (published is not None and published.tzinfo is not None
                and timedelta() <= now - published <= _WINDOW
                and notice == _notice(old_title, old_body, published)):
            return post_id
    return None
