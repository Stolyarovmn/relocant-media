"""Conservative, network-free freshness gate before any model calls.

This gate only decides whether review is worthwhile; ``keep`` is never permission
to publish. Unmarked historical dates are deliberately not event deadlines.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from relocant_media.domain.publication_time import publication_interval
from relocant_media.domain.road_work_time import overnight_road_windows, road_work_clauses


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
    r"|(?<!\d)(?P<numday>\d{1,2})\.[ \t]*(?P<nummonth>\d{1,2})\."
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
_DRAFT_SCHEDULE = re.compile(
    r"^(?:когда|время(?:\s+(?:работ|отключения|перекрытия|начала|окончания))?)\s*:\s*(.*)",
    re.IGNORECASE,
)
_DRAFT_CLOCK = re.compile(
    r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)"
    r"|(?<![\d.])(?:[01]?\d|2[0-3])\.[0-5]\d(?!\d|\.\s*\d)"
    r"|\b(?:в|до|к|с)\s+(?:[01]?\d|2[0-3])\s*час(?:а|ов)?\b",
    re.IGNORECASE,
)
_DRAFT_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DRAFT_EVENT = re.compile(
    r"\b(?:отключат|отключится|отключения?|перекроют|перекрытия?|закроют|"
    r"перенесут|возобновят|восстановят|останутся\s+без|будет\s+без|"
    r"пройд[её]т|пройдут|состоится|состоятся|начн[её]тся|начнутся|"
    r"заверш[аи]тся|завершатся|запланирован\w*|выступит|прочитает)\b",
    re.IGNORECASE,
)


def _draft_date_parts(text: str) -> list[tuple[int, int, int | None]]:
    text = re.sub(r"\b(\d{1,2})-го(?=\s)", r"\1", text, flags=re.IGNORECASE)
    parts = [
        (int(match["day"] or match["numday"]),
         _MONTH_LOOKUP[match["month"].lower()] if match["month"] else int(match["nummonth"]),
         int(year) if (year := match["year"] or match["numyear"]) else None)
        for match in _DATE.finditer(text)
    ]
    for match in _DRAFT_DATE_ISO.finditer(text):
        year, month, day = map(int, match.groups())
        parts.append((day, month, year))
    return parts


def _valid_draft_day(parts: tuple[int, int, int | None]) -> bool:
    day, month, year = parts
    try:
        date(year or 2000, month, day)
    except ValueError:
        return False
    return True


def _clock_fields_have_dates(title: str, body: str) -> bool:
    # Inline Telegram formatting and a line break after the heading are common in drafts.
    plain = re.sub(r"</?b>|\*|__|`", "", body, flags=re.IGNORECASE)
    lines = [line.strip(" \t•-") for line in plain.splitlines() if line.strip()]
    fields = []
    for index, line in enumerate(lines):
        if match := _DRAFT_SCHEDULE.match(line):
            field = match[1].strip()
            if not field and index + 1 < len(lines):
                next_line = lines[index + 1]
                if not re.match(r"^[А-Яа-яЁё ]+:", next_line):
                    field = next_line
            if _DRAFT_CLOCK.search(field):
                fields.append(field)
    if not fields:
        return True

    # A dated previous incident elsewhere in the body is not the date of this schedule.
    lead = lines[0] if lines and not _DRAFT_SCHEDULE.match(lines[0]) else ""
    lead = re.split(r"[!?;]|(?<=[А-Яа-яЁё])\.\s+", lead, maxsplit=1)[0]
    context = _draft_date_parts(title)
    if _DRAFT_EVENT.search(lead):
        context += _draft_date_parts(lead)
    dated = [_draft_date_parts(field) for field in fields]
    if any(not all(map(_valid_draft_day, parts)) for parts in dated):
        return False
    if all(dated):
        return True
    if not context or not all(map(_valid_draft_day, context)):
        return False
    all_days = [*context, *(part for parts in dated for part in parts)]
    return (len({(day, month) for day, month, _ in all_days}) == 1
            and len({year for _, _, year in all_days if year is not None}) <= 1)


def _relative_deadlines(text: str, published: datetime,
                        timezone_name: str = "Europe/Belgrade") -> list[date]:
    local_published = published.astimezone(ZoneInfo(timezone_name)).date()
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


def publication_date_context(
    published: datetime | None, precision: str = "unknown",
    *, timezone_name: str | None = None,
) -> dict[str, str | None]:
    if timezone_name is not None:
        zone = ZoneInfo(timezone_name)
        local = published.astimezone(zone) if published and published.tzinfo else None
        return {
            "timezone": timezone_name,
            "published_at_local": local.isoformat() if local and precision == "instant" else None,
            "published_on_local": local.date().isoformat() if local else None,
            "published_at_precision": precision,
            "tomorrow_local": (local.date() + timedelta(days=1)).isoformat() if local else None,
        }
    if published is None or published.tzinfo is None:
        return {"published_at_belgrade": None, "tomorrow_belgrade": None}
    local = published.astimezone(ZoneInfo("Europe/Belgrade"))
    return {
        "published_at_belgrade": local.isoformat() if precision == "instant" else None,
        "published_on_belgrade": local.date().isoformat(),
        "published_at_precision": precision,
        "tomorrow_belgrade": (local.date() + timedelta(days=1)).isoformat(),
    }


def draft_date_reason(title: str, body: str) -> str | None:
    if (re.search(r"\b(?:завтра|послезавтра|сегодня)\b", title + "\n" + body, re.IGNORECASE)
            or not _clock_fields_have_dates(title, body)):
        return "draft_requires_absolute_event_date"
    return None


def has_service_day(title: str, body: str) -> bool:
    """Find a date clue in a service notice for selection, without inferring an interval."""
    return bool(_DATE.search(f"{title}\n{body}") or _RELATIVE.search(f"{title}\n{body}"))


def has_draft_calendar_day(title: str, body: str) -> bool:
    return any(map(_valid_draft_day, _draft_date_parts(f"{title}\n{body}")))


# Explicit performance verbs also occur in announcements without the word "concert".
_DATED_PERFORMANCE = re.compile(
    r"\b(?:nastupa|nastupaju|nastupiće|nastupice|наступа|наступају|наступиће|выступит|выступят)\b",
    re.IGNORECASE,
)


def _event_deadlines(text: str, published: datetime) -> list[date]:
    deadlines: list[date] = []
    # Do not join separate sentences: an unrelated historical date must not
    # become a deadline merely because another sentence mentions an event.
    for clause in re.split(r"[\n;!?]|(?<=[а-яa-z])\.\s+", text.casefold()):
        performance = _DATED_PERFORMANCE.search(clause)
        if not (_EVENT.search(clause) or performance):
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
        elif len(dates) == 1 and (performance or _SINGLE_EVENT.search(clause)):
            deadlines.append(dates[0][0])
        # Multiple unrelated dates are ambiguous and grant no age extension.
    return deadlines


# #320: "трамваи снова ходят" через шесть часов — уже не новость. Маркер ищется только
# в заголовке и первой фразе, чтобы «ранее восстановлено, а с понедельника…» не терялось.
RESOLVED_DISRUPTION_AGE = timedelta(hours=2)
_RESOLVED = re.compile(
    r"(?<![^\W\d_])(?:"
    r"(?:ponovo|поново)\s+(?:saobraća|саобраћа|radi|ради|ima|има|stigl|стигл)"
    r"|uspostavljen|успостављен|normalizovan|нормализован"
    r"|(?:otklonjen|отклоњен)\w*\s+(?:kvar|квар)"
    r"|vraćen\w*\s+(?:u\s+)?redovn|враћен\w*\s+(?:у\s+)?редовн"
    r"|возобнов|восстановлен|снова\s+(?:ходят|ходит|идут|работает|есть|дали)"
    r")",
    re.IGNORECASE,
)


def resolved_disruption(title: str, body: str) -> bool:
    lead = re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0][:300]
    return bool(_RESOLVED.search(title) or _RESOLVED.search(lead))


def freshness_decision(
    title: str,
    body: str,
    published_at: datetime | None,
    now: datetime,
    *,
    horizon_hours: int = 48,
    published_at_precision: str = "unknown",
    timezone_name: str = "Europe/Belgrade",
) -> FreshnessDecision:
    """Use 24 hours for public-channel candidates, 48 for normal sources."""
    if horizon_hours not in (24, 48):
        raise ValueError("horizon_hours must be 24 or 48")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    if published_at is None or published_at.tzinfo is None:
        return FreshnessDecision("hold", "publication_date_unknown")
    published_at, interval_end = publication_interval(
        published_at, published_at_precision, timezone_name=timezone_name)
    age = now - published_at
    if age < timedelta(0):
        return FreshnessDecision("hold", "publication_date_future")
    if (published_at_precision == "day" and age > timedelta(days=7)
            and now - interval_end <= timedelta(days=7)):
        return FreshnessDecision("hold", "publication_day_straddles_max_age")
    if age > timedelta(days=7):
        return FreshnessDecision("discard", "publication_older_than_seven_days")
    text = f"{title}\n{body}"
    if age >= RESOLVED_DISRUPTION_AGE and resolved_disruption(title, body):
        return FreshnessDecision("discard", "disruption_already_resolved")
    local_published = published_at.astimezone(ZoneInfo(timezone_name))
    timed_ends: list[datetime] = []
    other_clauses = []
    for clause in road_work_clauses(text):
        if windows := overnight_road_windows(clause, local_published):
            timed_ends.extend(end for _, end in windows)
        else:
            other_clauses.append(clause)
    if any(end > now for end in timed_ends):
        return FreshnessDecision("keep", "dated_event_still_relevant")
    deadline_text = "\n".join(other_clauses) if timed_ends else text
    deadlines = _event_deadlines(deadline_text, local_published) + _relative_deadlines(
        deadline_text, published_at, timezone_name)
    local_date = now.astimezone(ZoneInfo(timezone_name)).date()
    if (deadlines or timed_ends) and all(deadline < local_date for deadline in deadlines):
        return FreshnessDecision("discard", "event_expired")
    if (published_at_precision == "day" and age > timedelta(hours=horizon_hours)
            and now - interval_end <= timedelta(hours=horizon_hours)
            and not any(deadline >= local_date for deadline in deadlines)):
        return FreshnessDecision("hold", "publication_day_straddles_news_window")
    if age > timedelta(hours=horizon_hours):
        if deadlines and any(deadline >= local_date for deadline in deadlines):
            return FreshnessDecision("keep", "dated_event_still_relevant")
        return FreshnessDecision("discard", "publication_outside_news_window")
    return FreshnessDecision("keep", "within_news_window")


GUIDE_SOURCE_TYPE = "official_guide"
GUIDE_MAX_AGE = timedelta(days=30)


def guide_freshness(verified_at: datetime | None, now: datetime) -> FreshnessDecision:
    """An evergreen guide is valid for 30 days after this text was first seen at the source.

    The guide text is bound to its content hash, so a changed page is a new item with a new
    verification date; an unchanged page simply expires and must be re-verified.
    """
    if verified_at is None or verified_at.tzinfo is None or verified_at > now:
        return FreshnessDecision("hold", "guide_verification_date_unknown")
    if now - verified_at > GUIDE_MAX_AGE:
        return FreshnessDecision("discard", "guide_verification_expired")
    return FreshnessDecision("keep", "guide_verified")


def evidence_freshness(
    title: str,
    body: str,
    published_at: datetime | None,
    now: datetime,
    *,
    precision: str,
    source_type: str,
    verified_at: datetime | None,
    timezone_name: str = "Europe/Belgrade",
) -> FreshnessDecision:
    """One decision point: guides use their verification date, everything else its news date."""
    if source_type == GUIDE_SOURCE_TYPE:
        if verified_at is not None and verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=UTC)
        return guide_freshness(verified_at, now)
    return freshness_decision(title, body, published_at, now, published_at_precision=precision,
                              timezone_name=timezone_name)
