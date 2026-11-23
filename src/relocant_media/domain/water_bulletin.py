"""Границы источника для ежедневного списка аварий и общей карты автоцистерн."""

import re
from urllib.parse import urlsplit

_CITY_WATER_PATH = re.compile(
    r"/(?:lat|cir)/servisne-informacije/vodovod-i-kanalizacija/a[1-9][0-9]*/[^/]+\.html\Z"
)
_DAILY_WATER_TITLE = re.compile(
    r"kvarovi na (?:vodovodnoj )?mre[žz]i\s+(?:za\s+\d|bi[ćc]e otklonjeni tokom dana)"
    r"|кварови на (?:водоводној )?мрежи\s+(?:за\s+\d|биће отклоњени током дана)",
    re.IGNORECASE,
)
_SHARED_TANKER_HEADING = re.compile(
    r"(?:raspored (?:auto[- ]?)?cistern[ei]|распоред (?:ауто[- ]?)?цистерн[еи])\s*[:.]?",
    re.IGNORECASE,
)
_TANKER_IN_DRAFT = re.compile(
    r"\b(?:а[ву]то[- ]?)?цистерн\w*"
    r"|\bподвоз\w*\s+(?:питьев\w*\s+)?вод\w*"
    r"|\bпитьев\w*\s+вод\w*\s+(?:привез\w*|подвез\w*|будут подвозить)",
    re.IGNORECASE,
)


def is_city_water_bulletin(url: str, title: str) -> bool:
    try:
        parts = urlsplit(url)
        return bool(
            parts.scheme == "https" and parts.hostname in {"beograd.rs", "www.beograd.rs"}
            and parts.port in {None, 443} and parts.username is None and parts.password is None
            and _CITY_WATER_PATH.fullmatch(parts.path) and _DAILY_WATER_TITLE.search(title)
        )
    except ValueError:
        return False


def is_shared_tanker_heading(line: str) -> bool:
    return bool(_SHARED_TANKER_HEADING.fullmatch(" ".join(line.split())))


def draft_water_tanker_reason(url: str, source_title: str, title: str, body: str) -> str | None:
    """Общий перечень автоцистерн не подтверждает подвоз воды на конкретный адрес."""
    if is_city_water_bulletin(url, source_title) and _TANKER_IN_DRAFT.search(f"{title}\n{body}"):
        return "water_tanker_scope_unverified"
    return None
