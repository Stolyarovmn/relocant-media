"""Explicit overnight date pairs and their shared clock range in road announcements."""

import re
from datetime import date, datetime, time, timedelta

_ROAD = re.compile(r"\b(?:saobra[ćc]|саобраћ|kolovoz|коловоз|tunel|тунел)", re.I)
_WORK = re.compile(r"\b(?:radov|радов|zatvar|затвар|zauzet|заузет|obustav|обустав)", re.I)
_DATE_PAIR = re.compile(
    r"(?<![\d.])(?P<day_from>\d{1,2})\.(?:(?P<month_from>\d{1,2})\.)?\s*/\s*"
    r"(?P<day_to>\d{1,2})\.(?P<month_to>\d{1,2})\."
    r"(?:\s*(?P<year_to>20\d{2})(?!\d))?",
)
_HOUR = r"(?:[01]?\d|2[0-3])"
_CLOCK_RANGE = re.compile(
    rf"\b(?:od|од)\s+({_HOUR})[.:]([0-5]\d)\s+(?:do|до)\s+({_HOUR})[.:]([0-5]\d)\b",
    re.I,
)


def overnight_road_windows(
    clause: str, published: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    """Accept consecutive dates and one unambiguous overnight clock range."""
    if not (_ROAD.search(clause) and _WORK.search(clause)):
        return ()
    clocks = _CLOCK_RANGE.findall(clause)
    if len(clocks) != 1:
        return ()
    start_hour, start_minute, end_hour, end_minute = map(int, clocks[0])
    start, end = time(start_hour, start_minute), time(end_hour, end_minute)
    if start <= end:
        return ()

    windows: set[tuple[datetime, datetime]] = set()
    for pair in _DATE_PAIR.finditer(clause):
        month = int(pair["month_to"])
        year = pair["year_to"]
        if year is None and abs(month - published.month) > 6:
            return ()
        try:
            last_day = date(int(year) if year else published.year, month, int(pair["day_to"]))
        except ValueError:
            return ()
        first_day = last_day - timedelta(days=1)
        if (first_day.day != int(pair["day_from"])
                or pair["month_from"] and first_day.month != int(pair["month_from"])):
            return ()
        windows.add((
            datetime.combine(first_day, start, tzinfo=published.tzinfo),
            datetime.combine(last_day, end, tzinfo=published.tzinfo),
        ))
    return tuple(sorted(windows))
