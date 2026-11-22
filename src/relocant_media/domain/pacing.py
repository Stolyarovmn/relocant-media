"""Ровный ритм ленты (#337): не выпускать несрочные посты пачкой.

Пауза между несрочными постами разгружает ленту. Вне утреннего окна она
снимается при близком истечении материала. Срочные поводы не ждут.
"""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from relocant_media.domain.quiet_hours import QUIET_END, QUIET_START, QUIET_ZONE, is_quiet_time

MIN_GAP = timedelta(minutes=20)
DEADLINE_MARGIN = timedelta(minutes=90)
URGENT_CATEGORIES = frozenset({"transport", "utilities", "weather"})
PACING_REASON = "pacing_min_gap"
# Решение владельца 22.11.2026 (#359): несрочное, готовое в ночную тишину
# (23:00–08:00 по Белграду), выходит утром с 08:00 с обычной паузой.
# Срок жизни отложенного на утро материала: время для раскладки паузой MIN_GAP.
MORNING_DEADLINE = time(12, 0)


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    raw_id: int
    published_at: datetime | None
    category: str = ""
    urgent: bool = False
    event_place: dict[str, Any] | None = None


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


def _in_morning_window(local_time: time) -> bool:
    return QUIET_END <= local_time < MORNING_DEADLINE


def _schedule_clock(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.utcoffset() is not None else None


def _event_relevant_today(place: dict[str, Any] | None, now: datetime) -> bool:
    if not isinstance(place, dict):
        return False
    slots = place.get("occurrences") if place.get("version") == 2 else [place]
    if not isinstance(slots, list):
        return False
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, QUIET_ZONE)
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = _schedule_clock(slot.get("starts_at"))
        if start is None or start >= tomorrow:
            continue
        raw_end = slot.get("ends_at")
        if raw_end is None and start >= now:
            return True
        end = _schedule_clock(raw_end)
        if end is not None and end > max(now, start):
            return True
    return False


def delivery_order(pending: Iterable[PendingDelivery], *, now: datetime) -> list[int]:
    """Утром сначала срочное и подтверждённые события на сегодня, затем свежие новости."""
    local = now.astimezone(QUIET_ZONE)
    if not _in_morning_window(local.time()):
        return sorted(row.raw_id for row in pending)

    def key(row: PendingDelivery) -> tuple[int, int]:
        if _is_urgent(row.category, row.urgent):
            return (0, row.raw_id)
        if _event_relevant_today(row.event_place, local):
            return (1, row.raw_id)
        published_at = row.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        if (published_at is not None and published_at <= now
                and published_at.astimezone(QUIET_ZONE).date() == local.date()):
            return (2, row.raw_id)
        return (3, row.raw_id)

    return [row.raw_id for row in sorted(pending, key=key)]


def pacing_hold(
    *, category: str, urgent: bool, now: datetime,
    last_sent_at: datetime | None, deadline_at: datetime | None,
) -> str | None:
    """Return a hold reason when a non-urgent post should wait for the next cycle."""
    if _is_urgent(category, urgent) or last_sent_at is None:
        return None
    if now - last_sent_at >= MIN_GAP:
        return None
    if (not _in_morning_window(now.astimezone(QUIET_ZONE).time())
            and (deadline_at is None or deadline_at - now <= DEADLINE_MARGIN)):
        return None
    return PACING_REASON
