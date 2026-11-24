"""Стабильные ссылки на объявления в публичной карте Белграда."""

import hashlib
import math
import re
from datetime import timedelta

from relocant_media.domain.dedup import canonicalize_url

MAP_ARCHIVE_AGE = timedelta(days=14)

_MAP_PAGE = "https://stolyarovmn.github.io/relocant-media/map/"
_TELEGRAM_BOT = "https://t.me/relocant_media_belgrade_bot"
_TELEGRAM_APPS = (_TELEGRAM_BOT, f"{_TELEGRAM_BOT}/map")
MAP_HOME_URL = f"{_TELEGRAM_BOT}?startapp"
_MAP_LINK_PREFIXES = (
    f"{_MAP_PAGE}?event=",
    f"{_MAP_PAGE}?mode=places&event=",
    *(f"{app}?startapp=" for app in _TELEGRAM_APPS),
    *(f"{app}?startapp=places_" for app in _TELEGRAM_APPS),
)
_EVENT_ID = re.compile(r"event_[0-9a-f]{20}")
_REPAIR_KEY = r"(-?[0-9]{1,9})_(-?[0-9]{1,9})"
_REPAIR_ROUTES = (
    re.compile(re.escape(f"{_MAP_PAGE}?event=") + _EVENT_ID.pattern + r"&repair=" + _REPAIR_KEY),
    *(re.compile(re.escape(f"{app}?startapp=repair_") + _EVENT_ID.pattern + "_" + _REPAIR_KEY)
      for app in _TELEGRAM_APPS),
)


def map_event_id(source_url: str) -> str:
    digest = hashlib.sha256(canonicalize_url(source_url).encode()).hexdigest()
    return f"event_{digest[:20]}"


def map_occurrence_id(source_url: str, key: str) -> str:
    """Сеанс имеет свой ID; ссылка поста сохраняет общий ID события."""
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", key):
        raise ValueError("invalid event occurrence key")
    digest = hashlib.sha256((canonicalize_url(source_url) + "\n" + key).encode()).hexdigest()
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


def map_repair_url(
    source_url: str, latitude: float, longitude: float, *, telegram_app: str | None = None,
) -> str:
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("invalid repair coordinates")
    # Keep the coordinate key identical to Math.round on the Mini App.
    lat, lon = (math.floor(value * 1_000_000 + 0.5) for value in (latitude, longitude))
    event = map_event_id(source_url)
    if telegram_app in _TELEGRAM_APPS:
        return f"{telegram_app}?startapp=repair_{event}_{lat}_{lon}"
    return f"{_MAP_PAGE}?event={event}&repair={lat}_{lon}"


def is_map_event_url(url: str) -> bool:
    if any(url.startswith(prefix) and _EVENT_ID.fullmatch(url[len(prefix):])
           for prefix in _MAP_LINK_PREFIXES):
        return True
    for route in _REPAIR_ROUTES:
        if match := route.fullmatch(url):
            lat, lon = map(int, match.groups())
            return abs(lat) <= 90_000_000 and abs(lon) <= 180_000_000
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
