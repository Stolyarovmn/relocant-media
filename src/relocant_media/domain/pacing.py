"""Ровный ритм ленты (#337): не выпускать несрочные посты пачкой.

Пауза между постами только откладывает несрочный материал до следующего цикла и
никогда не даёт ему сгореть: если до срока жизни материала осталось мало времени,
он выходит сразу. Срочные поводы (транспорт, отключения, погода) не ждут.
"""
from datetime import datetime, timedelta

MIN_GAP = timedelta(minutes=20)
DEADLINE_MARGIN = timedelta(minutes=90)
URGENT_CATEGORIES = frozenset({"transport", "utilities", "weather"})
PACING_REASON = "pacing_min_gap"


def pacing_hold(
    *, category: str, urgent: bool, now: datetime,
    last_sent_at: datetime | None, deadline_at: datetime | None,
) -> str | None:
    """Return a hold reason when a non-urgent post should wait for the next cycle."""
    if urgent or category.strip().lower() in URGENT_CATEGORIES or last_sent_at is None:
        return None
    if now - last_sent_at >= MIN_GAP:
        return None
    if deadline_at is None or deadline_at - now <= DEADLINE_MARGIN:
        return None
    return PACING_REASON
