"""Физическая афиша не выпускается молча без места в Mini App."""

import re

from relocant_media.domain.city_profile import load_city_profile, load_queue_place_aliases
from relocant_media.domain.editorial_products import EditorialPresentation

_PLACE = re.compile(
    r"(?im)^(?:площадка|где|адрес):|"
    r"\b(?:концерт\w*|выставк\w*|спектакл\w*|фестивал\w*|кинопоказ\w*|экскурси\w*)\b",
)
_REMOTE = re.compile(r"(?im)^формат:[ \t]*(?:полностью|только) онлайн[.! \t]*$")
_ATTENDANCE = re.compile(
    r"(?im)^(?:площадка|где|адрес|когда|билеты):|"
    r"\b(?:посетител\w*|посетить|состоится|пройд[её]т|начн[её]тся|билет\w*)\b",
)
_RECAP_TITLE = re.compile(
    r"\b(?:прош[её]л|прошла|состоялся|состоялась|завершился|завершилась)\b", re.I)
_AWAY_MATCH = re.compile(
    r"\b(?:выездн\w*\s+(?:\w+\s+)?(?:матч|встреч|игр)\w*|на выезде|в гостях)\b", re.I,
)
_BROADCAST = re.compile(r"\b(?:телетрансляц\w*|трансляц\w*|прямом эфире|по телевидению)\b", re.I)
_IN_PERSON = re.compile(
    r"(?im)^(?:площадка|где|адрес|билеты):|"
    r"\b(?:публичн\w*\s+(?:показ|просмотр)\w*|совместн\w*\s+просмотр\w*|"
    r"в\s+(?:баре|кафе)|фан[- ]?зон\w*|билет\w*|"
    r"вход\s+(?:свободный|бесплатный|по)|посетить)\b",
)


def _away_broadcast(city: str, title: str, body: str) -> bool:
    text = title + "\n" + body
    lead = title + "\n" + body.partition("\n\n")[0]
    if not _AWAY_MATCH.search(lead) or not _BROADCAST.search(text):
        return False
    try:
        aliases = (*load_city_profile(city).geography.city_aliases, *load_queue_place_aliases(city))
    except (OSError, ValueError):
        return False
    places = "|".join(re.escape(alias) for alias in aliases)
    return bool(places) and not re.search(rf"\b(?:в|на)\s+(?:{places})\w*", text, re.I)


def event_map_review_reason(
    presentation: EditorialPresentation | None, category: str, title: str, body: str,
    *, current_delivery: bool = False, city: str | None = None,
) -> str | None:
    # Исторические хеши и уже отправленные квитанции не пересчитываются.
    if not current_delivery and (presentation is None or presentation.version < 5):
        return None
    text = title + "\n" + body
    attendance = _ATTENDANCE.search(text) is not None
    in_person = category == "sport" and _IN_PERSON.search(text) is not None
    physical = category == "events" or (
        category == "culture" and _PLACE.search(text) is not None
        and (not _RECAP_TITLE.search(title) or attendance)
    ) or (category == "sport" and (attendance or in_person))
    if not physical or _REMOTE.search(body):
        return None
    if presentation is not None and (
        presentation.event_place is not None or presentation.native_map is not None
    ):
        return None
    if category == "sport" and city and not in_person and _away_broadcast(city, title, body):
        return None
    return "event_place_requires_verification"
