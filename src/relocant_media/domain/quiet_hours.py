"""Ночная тишина: посты в рабочий канал ночью уходят без звука уведомления."""
from collections.abc import Callable
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from relocant_media.domain.channel_targets import APPROVED_CHANNELS_BY_CITY

QUIET_ZONE = ZoneInfo("Europe/Belgrade")
QUIET_START = time(23, 0)
QUIET_END = time(8, 0)
_CHANNELS = frozenset(APPROVED_CHANNELS_BY_CITY.values())


def utc_now() -> datetime:
    return datetime.now(UTC)


def is_quiet_time(now: datetime) -> bool:
    """С 23:00 до 08:00 по Белграду, включая переход на летнее время."""
    if now.tzinfo is None:
        raise ValueError("quiet hours require an aware timestamp")
    local = now.astimezone(QUIET_ZONE).time()
    return local >= QUIET_START or local < QUIET_END


def silent_channel_post(chat_id: int | str, clock: Callable[[], datetime] | None = None) -> bool:
    """Только рабочий канал и только ночью; личные ответы и служебные чаты не меняются."""
    try:
        channel = int(chat_id)
    except (TypeError, ValueError):
        return False
    if channel not in _CHANNELS:
        return False
    return is_quiet_time((clock or utc_now)())
