"""Датированный прогноз Белграда из официальной таблицы РХМЗ."""

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

FORECAST_URL = "https://www.hidmet.gov.rs/ciril/prognoza/index.php"
WARNING_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-serbia"
BELGRADE_WEATHER_ZONE = ZoneInfo("Europe/Belgrade")
WEATHER_TIMEOUT = 12
_CAP = {"atom": "http://www.w3.org/2005/Atom", "cap": "urn:oasis:names:tc:emergency:cap:1.2"}
_BELGRADE = {"belgrade", "beograd", "београд", "city of belgrade"}
_HAZARDS = {"rain": "сильный дождь", "thunderstorm": "гроза", "wind": "сильный ветер",
            "snow/ice": "снег или гололёд", "fog": "туман", "high temperature": "жара",
            "low temperature": "сильный холод", "snow": "снег", "ice": "гололёд"}
_LEVELS = {"yellow": "жёлтый", "orange": "оранжевый", "red": "красный"}
_CONDITIONS = (
    "ясно", "солнечно", "преимущественно солнечно", "небольшая облачность",
    "ливни после полудня", "возможны ливни", "облачно", "небольшой дождь", "дождь",
    "переменная облачность", "ливень", "ливень с грозой", "мокрый снег", "небольшой снег",
    "снег", "ледяной дождь", "туман", "сильный ветер", "возможны кратковременные ливни",
    "неблагоприятная погода",
)


@dataclass(frozen=True)
class Forecast:
    text: str
    hazardous: bool


type WeatherStatus = Literal[
    "ok", "none_active", "partial", "invalid_payload", "stale", "future_timestamp",
    "today_missing", "http_error", "transport_error",
]


@dataclass(frozen=True)
class Weather:
    forecast: Forecast | None
    warning: str | None
    forecast_status: WeatherStatus
    warning_status: WeatherStatus

    def statuses(self) -> dict[str, WeatherStatus]:
        return {"rhmz_forecast": self.forecast_status, "meteoalarm": self.warning_status}


def _warning_reading(content: bytes, now: datetime) -> tuple[str | None, WeatherStatus]:
    if (now.utcoffset() is None or len(content) > 600_000
            or re.search(rb"<!\s*(?:doctype|entity)", content, re.I)):
        return None, "invalid_payload"
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, LookupError, ValueError):
        return None, "invalid_payload"
    if root.tag != f"{{{_CAP['atom']}}}feed":
        return None, "invalid_payload"
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    alerts = []
    unreadable = False
    for item in root.findall("atom:entry", _CAP):
        areas = {x.text.strip().casefold() for x in item.findall("cap:areaDesc", _CAP) if x.text}
        if not areas:
            unreadable = True
        if not areas.intersection(_BELGRADE):
            continue
        try:
            start = datetime.fromisoformat(item.findtext("cap:effective", "", _CAP))
            end = datetime.fromisoformat(item.findtext("cap:expires", "", _CAP))
        except ValueError:
            unreadable = True
            continue
        if start.utcoffset() is None or end.utcoffset() is None or start >= end:
            unreadable = True
            continue
        if start >= tomorrow or now >= end:
            continue
        event, _, level = item.findtext("cap:event", "", _CAP).casefold().rpartition(" ")
        if event not in _HAZARDS or level not in _LEVELS:
            unreadable = True
            continue
        try:
            starts = start.astimezone(now.tzinfo)
            ends = end.astimezone(now.tzinfo)
        except (OverflowError, ValueError):
            unreadable = True
            continue
        period = (f"{starts:%H:%M}–{ends:%H:%M}" if starts.date() == ends.date() else
                  f"{starts:%d.%m %H:%M}–{ends:%d.%m %H:%M}")
        text = f"РХМЗ предупреждает: {_HAZARDS[event]}, {_LEVELS[level]} уровень, {period}."
        alerts.append((tuple(_LEVELS).index(level), text))
    if alerts:
        return max(alerts)[1], "partial" if unreadable else "ok"
    return None, "invalid_payload" if unreadable else "none_active"


def _forecast_reading(html: str, now: datetime) -> tuple[Forecast | None, WeatherStatus]:
    if len(html) > 600_000 or now.utcoffset() is None:
        return None, "invalid_payload"
    page = BeautifulSoup(html, "html.parser")
    rows = [row.find_all(("td", "th"), recursive=False) for row in page.find_all("tr")]
    matches = [cells for cells in rows if cells and cells[0].get_text(strip=True) == "Београд"]
    if len(matches) != 1:
        return None, "invalid_payload"
    cells = matches[0]
    table = cells[0].find_parent("table")
    if table is None:
        return None, "invalid_payload"
    updated = re.search(r"Прогноза ажурирана:\s*(\d{2}\.\d{2}\.)\s*(\d{2}:\d{2})",
                        table.get_text(" ", strip=True))
    if updated is None:
        return None, "invalid_payload"
    try:
        stamp = datetime.strptime(f"{now.year} {updated[1]} {updated[2]}", "%Y %d.%m. %H:%M")
    except ValueError:
        return None, "invalid_payload"
    stamp = stamp.replace(tzinfo=now.tzinfo)
    if stamp > now and now.month == 1 and stamp.month == 12:
        stamp = stamp.replace(year=now.year - 1)
    if stamp > now:
        return None, "future_timestamp"
    if now - stamp > timedelta(hours=30):
        return None, "stale"
    has_day_columns = False
    for row in table.find_all("tr"):
        offset = 0
        for cell in row.find_all(("td", "th"), recursive=False):
            colspan = cell.get("colspan", "1")
            if not isinstance(colspan, str) or colspan not in {"1", "2", "3", "9", "10"}:
                break
            width = int(colspan)
            day = re.match(r"(\d{2}\.\d{2}\.)\s+", cell.get_text(" ", strip=True))
            has_day_columns = has_day_columns or day is not None
            if day and day[1] == now.strftime("%d.%m."):
                if width not in {2, 3} or offset == 0 or offset + width > len(cells):
                    return None, "invalid_payload"
                value = cells[offset + width - 2].get_text(strip=True)
                icon = cells[offset + width - 1].find("img")
                source = icon.get("src", "") if icon is not None else ""
                code = re.search(r"/prognoza/(\d{1,2})\.gif$", str(source))
                if (not re.fullmatch(r"-?\d{1,2}", value) or not -50 <= int(value) <= 60
                        or code is None or not 1 <= int(code[1]) <= len(_CONDITIONS)):
                    return None, "invalid_payload"
                condition = int(code[1])
                return Forecast(f"Днём до {int(value):+d} °C, {_CONDITIONS[condition - 1]}.",
                                condition in {12, 16, 18, 20}), "ok"
            offset += width
    return None, "today_missing" if has_day_columns else "invalid_payload"


def parse_warning(content: bytes, now: datetime) -> str | None:
    return _warning_reading(content, now)[0]


def parse_forecast(html: str, now: datetime) -> Forecast | None:
    return _forecast_reading(html, now)[0]


async def _read_source[T](
    client: httpx.AsyncClient, url: str,
    parse: Callable[[httpx.Response], tuple[T | None, WeatherStatus]],
) -> tuple[T | None, WeatherStatus]:
    try:
        async with asyncio.timeout(WEATHER_TIMEOUT):
            response = await client.get(url, timeout=WEATHER_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPStatusError:
        return None, "http_error"
    except (httpx.HTTPError, TimeoutError):
        return None, "transport_error"
    return parse(response)


async def collect_weather(client: httpx.AsyncClient, now: datetime) -> Weather:
    if now.utcoffset() is None:
        raise ValueError("weather check requires an aware timestamp")
    local = now.astimezone(BELGRADE_WEATHER_ZONE)
    (forecast, forecast_status), (warning, warning_status) = await asyncio.gather(
        _read_source(
            client, FORECAST_URL, lambda response: _forecast_reading(response.text, local),
        ),
        _read_source(
            client, WARNING_URL, lambda response: _warning_reading(response.content, local),
        ),
    )
    return Weather(forecast, warning, forecast_status, warning_status)
