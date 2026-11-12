"""Стабильные ссылки на объявления в публичной карте Белграда."""

import hashlib
import re

from relocant_media.domain.dedup import canonicalize_url

_MAP_PAGE = "https://stolyarovmn.github.io/relocant-media/map/"
_TELEGRAM_BOT = "https://t.me/relocant_media_belgrade_bot"
_TELEGRAM_APPS = (_TELEGRAM_BOT, f"{_TELEGRAM_BOT}/map")
_MAP_LINK_PREFIXES = (
    f"{_MAP_PAGE}?event=",
    f"{_MAP_PAGE}?mode=places&event=",
    *(f"{app}?startapp=" for app in _TELEGRAM_APPS),
    *(f"{app}?startapp=places_" for app in _TELEGRAM_APPS),
)
_EVENT_ID = re.compile(r"event_[0-9a-f]{20}")


def map_event_id(source_url: str) -> str:
    digest = hashlib.sha256(canonicalize_url(source_url).encode()).hexdigest()
    return f"event_{digest[:20]}"


def map_event_url(
    source_url: str, *, telegram_app: str | None = None, places: bool = False,
) -> str:
    event = map_event_id(source_url)
    if telegram_app in _TELEGRAM_APPS:
        prefix = "places_" if places else ""
        return f"{telegram_app}?startapp={prefix}{event}"
    mode = "mode=places&" if places else ""
    return f"{_MAP_PAGE}?{mode}event={event}"


def is_map_event_url(url: str) -> bool:
    return any(url.startswith(prefix) and _EVENT_ID.fullmatch(url[len(prefix):])
               for prefix in _MAP_LINK_PREFIXES)
