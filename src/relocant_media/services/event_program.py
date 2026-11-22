"""Программа фестиваля из явных абзацев источника и проверенного реестра города."""

import json
import re
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.city_profile import DEFAULT_CITIES_ROOT, CityEditorialProfile
from relocant_media.domain.source_locations import is_public_https_url

MONTHS = (
    "јануара", "фебруара", "марта", "априла", "маја", "јуна",
    "јула", "августа", "септембра", "октобра", "новембра", "децембра",
)
ORDINALS = {
    "првог": 1, "другог": 2, "трећег": 3, "четвртог": 4,
    "петог": 5, "шестог": 6, "седмог": 7,
}
_DATE = re.compile(
    r"^(првог|другог|трећег|четвртог|петог|шестог|седмог|завршног) "
    r"(дана фестивала|" + "|".join(MONTHS) + r"),", re.I,
)
_CLOCK = re.compile(r"\bод (\d{1,2})(?:[.:](\d{2}))? час(?:ова|а)?\b", re.I)


def registered_event_venues(profile: CityEditorialProfile) -> list[dict[str, Any]]:
    path = DEFAULT_CITIES_ROOT / profile.city_id / "event-venues.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    if registry["version"] != 1 or not isinstance(registry["venues"], list):
        raise ValueError("invalid event venue registry")
    return cast(list[dict[str, Any]], registry["venues"])


def festival_program(
    document: EvidenceDocument, *, profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    """Неполная/новая площадка, конфликт даты или новый формат закрывают весь план."""
    text = " ".join(document.body.split()).casefold()
    period = re.search(r"од (\d{1,2})\. до (\d{1,2})\. (" + "|".join(MONTHS) + r")",
                       document.title.casefold())
    count = re.search(r"на (\d+|пет) локација", text)
    if ("фестивал" not in text or period is None or count is None
            or document.published_at is None
            or re.search(r"отказан|одложен|премештен", text)):
        return None
    try:
        venues = registered_event_venues(profile)
        published = document.published_at.astimezone(ZoneInfo(profile.timezone))
        month = MONTHS.index(period[3]) + 1
        first = datetime(published.year, month, int(period[1]))
        last = datetime(published.year, month, int(period[2]))
        if not timedelta(0) <= first - published.replace(tzinfo=None) <= timedelta(days=120):
            return None
        if not timedelta(1) <= last - first <= timedelta(days=30):
            return None
        slots = []
        used = set()
        for paragraph in document.body.splitlines():
            paragraph = " ".join(paragraph.casefold().split())
            date = _DATE.match(paragraph)
            if date is None:
                continue
            date_day = (last.day if date[1] == "завршног" else
                        first.day + ORDINALS[date[1]] - 1 if date[2] == "дана фестивала"
                        else ORDINALS[date[1]])
            if date[2] != "дана фестивала" and date[2] != period[3]:
                return None
            clocks = list(_CLOCK.finditer(paragraph))
            matches = [venue for venue in venues if any(
                alias.casefold() in paragraph for alias in venue["aliases"]
            )]
            if len(matches) != 1 or not clocks:
                return None
            first_action = re.search(r"наступи|очекује|биће одржан", paragraph)
            if first_action is None or clocks[0].start() > first_action.start():
                return None
            venue = matches[0]
            clock = clocks[0]
            start = datetime(first.year, month, date_day, int(clock[1]), int(clock[2] or 0))
            if not first.date() <= start.date() <= last.date():
                return None
            if any(int(c[1]) * 60 + int(c[2] or 0) < start.hour * 60 + start.minute
                   for c in clocks):
                return None
            if not all(is_public_https_url(venue[key]) for key in (
                "coordinate_source_url", "venue_source_url",
            )):
                return None
            if (not 44.2 <= venue["venue"]["latitude"] <= 45.2
                    or not 19.8 <= venue["venue"]["longitude"] <= 21.1):
                return None
            used.add(venue["id"])
            slots.append({
                "event_name": document.title,
                "occurrence_key": f"{venue['id']}-{start:%Y%m%d-%H%M}",
                "venue": venue["venue"], "venue_note": venue.get("note"),
                "starts_at": start.isoformat(), "ends_at": None,
                "time_basis": "source_festival_program",
                "schedule_source_url": document.url,
                "coordinate_source_url": venue["coordinate_source_url"],
                "venue_source_url": venue["venue_source_url"],
                "source_quote": paragraph,
            })
        expected_count = 5 if count[1] == "пет" else int(count[1])
        if len(used) != expected_count or len(slots) != (last.date() - first.date()).days + 1:
            return None
        if len({slot["starts_at"][:10] for slot in slots}) != len(slots):
            return None
        return {"occurrences": slots}
    except (OSError, ValueError, TypeError, KeyError):
        return None
