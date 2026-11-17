"""Физическая афиша не выпускается молча без места в Mini App."""

import re

from relocant_media.domain.editorial_products import EditorialPresentation

_PLACE = re.compile(
    r"(?im)^(?:площадка|где|адрес):|"
    r"\b(?:концерт\w*|выставк\w*|спектакл\w*|фестивал\w*|кинопоказ\w*|экскурси\w*)\b",
)
_REMOTE = re.compile(r"(?im)^формат:[ \t]*(?:полностью|только) онлайн[.! \t]*$")


def event_map_review_reason(
    presentation: EditorialPresentation | None, category: str, title: str, body: str,
    *, current_delivery: bool = False,
) -> str | None:
    # Исторические хеши и уже отправленные квитанции не пересчитываются.
    if not current_delivery and (presentation is None or presentation.version < 5):
        return None
    physical = category == "events" or (
        category == "culture" and _PLACE.search(title + "\n" + body) is not None
    )
    if not physical or _REMOTE.search(body):
        return None
    if presentation is not None and (
        presentation.event_place is not None or presentation.native_map is not None
    ):
        return None
    return "event_place_requires_verification"
