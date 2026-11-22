"""Publication date precision, separate from a stored scheduling boundary."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

BELGRADE = ZoneInfo("Europe/Belgrade")
PRECISIONS = {"unknown", "day", "instant"}


def local_clock_precision(value: datetime) -> str:
    """A wall time at a clock change does not identify one instant."""
    return ("instant" if value.utcoffset() is not None
            and value.replace(fold=0).utcoffset() == value.replace(fold=1).utcoffset()
            else "unknown")


def same_legacy_publication_time(
    previous: datetime | None, previous_precision: str,
    current: datetime | None, current_precision: str,
) -> bool:
    """Support newly labelled timestamps without changing frozen evidence."""
    return (previous_precision == "unknown" and current_precision in {"day", "instant"}
            and previous is not None and previous.utcoffset() is not None
            and current is not None and current.utcoffset() is not None
            and previous.astimezone(UTC) == current.astimezone(UTC))


def publication_interval(value: datetime, precision: str, *,
                         timezone_name: str = "Europe/Belgrade") -> tuple[datetime, datetime]:
    if precision not in PRECISIONS or value.tzinfo is None:
        raise ValueError("valid publication precision and timezone required")
    if precision != "day":
        return value.astimezone(UTC), value.astimezone(UTC)
    zone = ZoneInfo(timezone_name)
    day = value.astimezone(zone).date()
    lower = datetime.combine(day, time.min, zone)
    upper = datetime.combine(day + timedelta(days=1), time.min, zone)
    return lower.astimezone(UTC), upper.astimezone(UTC)
