"""Ровный ритм ленты (#337): не выпускать несрочные посты пачкой.

Пауза между постами только откладывает несрочный материал до следующего цикла и
никогда не даёт ему сгореть: если до срока жизни материала осталось мало времени,
он выходит сразу. Срочные поводы (транспорт, отключения, погода) не ждут.
"""
from collections.abc import Iterable
from datetime import UTC, datetime, time, timedelta

from relocant_media.domain.quiet_hours import QUIET_END, QUIET_START, QUIET_ZONE, is_quiet_time

MIN_GAP = timedelta(minutes=20)
DEADLINE_MARGIN = timedelta(minutes=90)
URGENT_CATEGORIES = frozenset({"transport", "utilities", "weather"})
PACING_REASON = "pacing_min_gap"
# Решение владельца 22.11.2026 (#359): несрочное, готовое в ночную тишину
# (23:00–08:00 по Белграду), выходит утром с 08:00 с обычной паузой.
# Срок жизни отложенного на утро материала: время для раскладки паузой MIN_GAP.
MORNING_DEADLINE = time(12, 0)


def _is_urgent(category: str, urgent: bool) -> bool:
    return urgent or category.strip().lower() in URGENT_CATEGORIES


def morning_release(*, category: str, urgent: bool, now: datetime) -> datetime | None:
    """Return the next 08:00 Belgrade for a non-urgent post ready during the night window."""
    if _is_urgent(category, urgent):
        return None
    if not is_quiet_time(now):
        return None
    local = now.astimezone(QUIET_ZONE)
    day = local.date() + timedelta(days=1) if local.time() >= QUIET_START else local.date()
    return datetime.combine(day, QUIET_END, QUIET_ZONE)


def morning_deadline(release: datetime) -> datetime:
    return datetime.combine(release.astimezone(QUIET_ZONE).date(), MORNING_DEADLINE, QUIET_ZONE)


def delivery_order(
    pending: Iterable[tuple[int, datetime | None]], *, now: datetime,
) -> list[int]:
    """Order pending posts: today's Belgrade news first, then by arrival (#359).

    With a 20-minute gap the morning backlog goes out one by one, so yesterday's
    non-urgent note must not take the first morning slot before today's news.
    During the day everything is "today" and the order stays by arrival.
    """
    today = now.astimezone(QUIET_ZONE).date()

    def key(row: tuple[int, datetime | None]) -> tuple[bool, int]:
        raw_id, published_at = row
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        is_today = (published_at is not None
                    and published_at.astimezone(QUIET_ZONE).date() == today)
        return (not is_today, raw_id)

    return [raw_id for raw_id, _ in sorted(pending, key=key)]


def pacing_hold(
    *, category: str, urgent: bool, now: datetime,
    last_sent_at: datetime | None, deadline_at: datetime | None,
) -> str | None:
    """Return a hold reason when a non-urgent post should wait for the next cycle."""
    if _is_urgent(category, urgent) or last_sent_at is None:
        return None
    if now - last_sent_at >= MIN_GAP:
        return None
    if deadline_at is None or deadline_at - now <= DEADLINE_MARGIN:
        return None
    return PACING_REASON
