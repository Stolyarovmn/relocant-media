"""Физическая афиша не выпускается молча без места в Mini App."""

import re

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


def event_map_review_reason(
    presentation: EditorialPresentation | None, category: str, title: str, body: str,
    *, current_delivery: bool = False,
) -> str | None:
    # Исторические хеши и уже отправленные квитанции не пересчитываются.
    if not current_delivery and (presentation is None or presentation.version < 5):
        return None
    text = title + "\n" + body
    attendance = _ATTENDANCE.search(text) is not None
    physical = category == "events" or (
        category == "culture" and _PLACE.search(text) is not None
        and (not _RECAP_TITLE.search(title) or attendance)
    ) or (category == "sport" and attendance)
    if not physical or _REMOTE.search(body):
        return None
    if presentation is not None and (
        presentation.event_place is not None or presentation.native_map is not None
    ):
        return None
    return "event_place_requires_verification"
