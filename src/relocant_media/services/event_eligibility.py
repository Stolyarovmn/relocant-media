"""Conservative preliminary date gate; never grants publication permission."""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

_MONTH_GROUPS = (
    "januar january januara января јануар јануара",
    "februar february februara февраля фебруар фебруара",
    "mart march marta марта март марта",
    "april aprila апреля април априла",
    "maj may maja мая мај маја",
    "jun june juna июня јун јуна",
    "jul july jula июля јул јула",
    "avgust august avgusta августа август августа",
    "septembar september septembra сентября септембар септембра",
    "oktobar october oktobra октября октобар октобра",
    "novembar november novembra ноября новембар новембра",
    "decembar december decembra декабря децембар децембра",
)
_MONTHS = {
    name: number
    for number, names in enumerate(_MONTH_GROUPS, 1)
    for name in names.split()
}
_NAMED_DATE = re.compile(
    r"\b(\d{1,2})\.?\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def event_content_reason(title: str, body: str, *, now: datetime) -> str | None:
    """Reject undated/expired announcements using source text only.

    A passing result merely allows factual reviews. Those reviews must associate
    the date with the actual event and verify venue, price and attendance terms.
    Publication metadata must not be appended to ``body`` by callers.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(ZoneInfo("Europe/Belgrade")).date()
    text = f"{title}\n{body}".lower()
    parts: list[tuple[int, int, int]] = []
    for match in _NUMERIC_DATE.finditer(text):
        day, month, year = map(int, match.groups())
        parts.append((year, month, day))
    for match in _ISO_DATE.finditer(text):
        iso_year, iso_month, iso_day = map(int, match.groups())
        parts.append((iso_year, iso_month, iso_day))
    for match in _NAMED_DATE.finditer(text):
        day_text, month_name, year_text = match.groups()
        parts.append(
            (int(year_text) if year_text else today.year, _MONTHS[month_name], int(day_text))
        )
    dates: list[date] = []
    for year, month, day in parts:
        try:
            dates.append(date(year, month, day))
        except ValueError:
            continue
    if not dates:
        return "venue_event_date_missing"
    if not any(day >= today for day in dates):
        return "venue_event_expired"
    return None
