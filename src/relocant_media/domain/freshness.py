"""Conservative, network-free freshness gate before any model calls.

This gate only decides whether review is worthwhile; ``keep`` is never permission
to publish. Unmarked historical dates are deliberately not event deadlines.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FreshnessDecision:
    action: Literal["keep", "discard", "hold"]
    reason: str


_MONTHS = {
    1: ("januar", "јануар", "январ"),
    2: ("februar", "фебруар", "феврал"),
    3: ("mart", "март"),
    4: ("april", "април", "апрел"),
    5: ("maj", "мај", "май", "мая"),
    6: ("jun", "јун", "июн"),
    7: ("jul", "јул", "июл"),
    8: ("avgust", "август"),
    9: ("septembar", "septembr", "септембар", "септембр", "сентябр"),
    10: ("oktobar", "oktobr", "октобар", "октобр", "октябр"),
    11: ("novembar", "novembr", "новембар", "новембр", "ноябр"),
    12: ("decembar", "decembr", "децембар", "децембр", "декабр"),
}
_MONTH_LOOKUP = {stem: number for number, stems in _MONTHS.items() for stem in stems}
_MONTH_PATTERN = "|".join(sorted(_MONTH_LOOKUP, key=len, reverse=True))
_DATE = re.compile(
    rf"(?<!\d)(?P<day>\d{{1,2}})\.?\s*(?P<month>{_MONTH_PATTERN})"
    r"[a-zа-яёј]*\b(?:\s+(?P<year>20\d{2})\b)?"
    r"|(?<!\d)(?P<numday>\d{1,2})\.(?P<nummonth>\d{1,2})\."
    r"\s*(?P<numyear>20\d{2})\b",
    re.IGNORECASE,
)
# Only clauses that explicitly describe an event, work, or deadline qualify.
_EVENT = re.compile(
    r"predstav|представ|спектакл|koncert|концерт|festival|фестивал|"
    r"održa|одржа|состоится|пройдет|пройдёт|radovi|радови|работ[ыа]|"
    r"zatvor|затвор|закрыт|obustav|обустав|ограничен|"
    r"prijav|пријав|заяв[окк]|rok\b|рок\b|до\s+\d|do\s+\d",
    re.IGNORECASE,
)
_DEADLINE = re.compile(r"(?:\bdo|\bдо|\buntil)\s*$", re.IGNORECASE)
_RANGE = re.compile(r"(?:-|–|—|\bdo|\bдо)\s*$", re.IGNORECASE)
_SINGLE_EVENT = re.compile(
    r"predstav|представ|спектакл|koncert|концерт|festival|фестивал|"
    r"održa|одржа|состоится|пройдет|пройдёт|"
    r"završ|заврш|заверш|оконч|истека|istič|истич",
    re.IGNORECASE,
)
_SERVICE = re.compile(
    r"bez\s+vode|без\s+вод[еы]|отключ|isklju[čc]|искључ|"
    r"vodosnabdev|водоснаб|без\s+струје|bez\s+struje|снабдева",
    re.IGNORECASE,
)
_RELATIVE = re.compile(
    r"\b(prekosutra|прекосутра|послезавтра|sutra|сутра|завтра|"
    r"danas|данас|сегодня)\b",
    re.IGNORECASE,
)


def _relative_deadlines(text: str, published: datetime) -> list[date]:
    local_published = published.astimezone(ZoneInfo("Europe/Belgrade")).date()
    offsets = {
        "prekosutra": 2,
        "прекосутра": 2,
        "послезавтра": 2,
        "sutra": 1,
        "сутра": 1,
        "завтра": 1,
        "danas": 0,
        "данас": 0,
        "сегодня": 0,
    }
    result: list[date] = []
    for clause in re.split(r"[\n;!?]|(?<=[а-яa-z])\.\s+", text.casefold()):
        if not (_SERVICE.search(clause) or _EVENT.search(clause)):
            continue
        # Relative dates belong to the source's publication day, never the run day.
        result.extend(
            local_published + timedelta(days=offsets[m[0]]) for m in _RELATIVE.finditer(clause)
        )
    return result


def publication_date_context(published: datetime | None) -> dict[str, str | None]:
    if published is None or published.tzinfo is None:
        return {"published_at_belgrade": None, "tomorrow_belgrade": None}
    local = published.astimezone(ZoneInfo("Europe/Belgrade"))
    return {
        "published_at_belgrade": local.isoformat(),
        "tomorrow_belgrade": (local.date() + timedelta(days=1)).isoformat(),
    }


def draft_date_reason(title: str, body: str) -> str | None:
    if re.search(r"\b(?:завтра|послезавтра|сегодня)\b", title + "\n" + body, re.IGNORECASE):
        return "draft_requires_absolute_event_date"
    return None


def _event_deadlines(text: str, published: datetime) -> list[date]:
    deadlines: list[date] = []
    # Do not join separate sentences: an unrelated historical date must not
    # become a deadline merely because another sentence mentions an event.
    for clause in re.split(r"[\n;!?]|(?<=[а-яa-z])\.\s+", text.casefold()):
        if not _EVENT.search(clause):
            continue
        matches = list(_DATE.finditer(clause))
        dates: list[tuple[date, int, int]] = []
        for match in matches:
            day = int(match["day"] or match["numday"])
            month = (
                _MONTH_LOOKUP[match["month"].casefold()]
                if match["month"]
                else int(match["nummonth"])
            )
            explicit_year = match["year"] or match["numyear"]
            year = int(explicit_year) if explicit_year else published.year
            if not explicit_year and abs(month - published.month) > 6:
                # Ambiguous near New Year: do not invent a year.
                continue
            try:
                value = date(year, month, day)
            except ValueError:
                continue
            dates.append((value, match.start(), match.end()))
        # A range's last endpoint or an explicit deadline is stronger than
        # its start. Single dated event clauses also provide an exact day.
        endpoints = [
            value
            for value, start, _ in dates
            if _DEADLINE.search(clause[:start]) or _RANGE.search(clause[:start])
        ]
        if endpoints:
            deadlines.extend(endpoints)
        elif len(dates) == 1 and _SINGLE_EVENT.search(clause):
            deadlines.append(dates[0][0])
        # Multiple unrelated dates are ambiguous and grant no age extension.
    return deadlines


def freshness_decision(
    title: str,
    body: str,
    published_at: datetime | None,
    now: datetime,
    *,
    horizon_hours: int = 48,
) -> FreshnessDecision:
    """Use 24 hours for public-channel candidates, 48 for normal sources."""
    if horizon_hours not in (24, 48):
        raise ValueError("horizon_hours must be 24 or 48")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    if published_at is None or published_at.tzinfo is None:
        return FreshnessDecision("hold", "publication_date_unknown")
    published_at = published_at.astimezone(UTC)
    age = now - published_at
    if age < timedelta(0):
        return FreshnessDecision("hold", "publication_date_future")
    if age > timedelta(days=7):
        return FreshnessDecision("discard", "publication_older_than_seven_days")
    text = f"{title}\n{body}"
    deadlines = _event_deadlines(text, published_at) + _relative_deadlines(text, published_at)
    local_date = now.astimezone(ZoneInfo("Europe/Belgrade")).date()
    if deadlines and all(deadline < local_date for deadline in deadlines):
        return FreshnessDecision("discard", "event_expired")
    if age > timedelta(hours=horizon_hours):
        if deadlines and any(deadline >= local_date for deadline in deadlines):
            return FreshnessDecision("keep", "dated_event_still_relevant")
        return FreshnessDecision("discard", "publication_outside_news_window")
    return FreshnessDecision("keep", "within_news_window")
