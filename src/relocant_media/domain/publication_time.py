"""Publication date precision, separate from a stored scheduling boundary."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

BELGRADE = ZoneInfo("Europe/Belgrade")
PRECISIONS = {"unknown", "day", "instant"}


def publication_interval(value: datetime, precision: str) -> tuple[datetime, datetime]:
    if precision not in PRECISIONS or value.tzinfo is None:
        raise ValueError("valid publication precision and timezone required")
    if precision != "day":
        return value.astimezone(UTC), value.astimezone(UTC)
    day = value.astimezone(BELGRADE).date()
    lower = datetime.combine(day, time.min, BELGRADE)
    upper = datetime.combine(day + timedelta(days=1), time.min, BELGRADE)
    return lower.astimezone(UTC), upper.astimezone(UTC)
