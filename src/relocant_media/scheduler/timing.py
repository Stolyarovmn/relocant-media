import re
from datetime import datetime, timedelta, timezone

_INTERVAL = re.compile(r"^([1-9][0-9]*)([mhd])$")


def parse_interval(value: str) -> timedelta:
    match = _INTERVAL.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"unsupported interval: {value}")
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    return timedelta(days=amount)


def bucket_start(value: datetime, interval: timedelta) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("bucket timestamp must be timezone-aware")
    seconds = int(interval.total_seconds())
    if seconds <= 0:
        raise ValueError("interval must be positive")
    timestamp = int(value.astimezone(timezone.utc).timestamp())
    bucket = timestamp - (timestamp % seconds)
    return datetime.fromtimestamp(bucket, tz=timezone.utc)


def retry_backoff(attempt: int) -> timedelta:
    if attempt <= 0:
        raise ValueError("attempt must be positive")
    minutes = min(60, 2 ** min(attempt - 1, 6))
    return timedelta(minutes=minutes)
