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
    if any(url.startswith(prefix) and _EVENT_ID.fullmatch(url[len(prefix):])
           for prefix in _MAP_LINK_PREFIXES):
        return True
    prefixes = (f"{_MAP_PAGE}?events=", *(f"{app}?startapp=digest_" for app in _TELEGRAM_APPS))
    for prefix in prefixes:
        if url.startswith(prefix):
            ids = url[len(prefix):].split("~" if prefix.endswith("=") else "-")
            return 2 <= len(ids) <= 4 and all(_EVENT_ID.fullmatch(value) for value in ids)
    return False


def map_digest_url(ids: tuple[str, ...], *, telegram_app: str | None = None) -> str | None:
    if not ids:
        return None
    if len(ids) > 4 or not all(_EVENT_ID.fullmatch(value) for value in ids):
        raise ValueError("invalid digest map IDs")
    if len(ids) == 1:
        return (f"{telegram_app}?startapp={ids[0]}" if telegram_app in _TELEGRAM_APPS else
                f"{_MAP_PAGE}?event={ids[0]}")
    if telegram_app in _TELEGRAM_APPS:
        return f"{telegram_app}?startapp=digest_{'-'.join(ids)}"
    return f"{_MAP_PAGE}?events={'~'.join(ids)}"
