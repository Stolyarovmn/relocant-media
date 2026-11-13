"""Датированный прогноз Белграда из официальной таблицы РХМЗ."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from xml.etree import ElementTree

from bs4 import BeautifulSoup

FORECAST_URL = "https://www.hidmet.gov.rs/ciril/prognoza/index.php"
WARNING_URL = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-serbia"
_CAP = {"atom": "http://www.w3.org/2005/Atom", "cap": "urn:oasis:names:tc:emergency:cap:1.2"}
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


def parse_warning(content: bytes, now: datetime) -> str | None:
    if len(content) > 600_000 or re.search(rb"<!\s*(?:doctype|entity)", content, re.I):
        return None
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    alerts = []
    for item in root.findall("atom:entry", _CAP):
        areas = {x.text.strip().casefold() for x in item.findall("cap:areaDesc", _CAP) if x.text}
        if not areas.intersection({"belgrade", "beograd", "београд", "city of belgrade"}):
            continue
        event, _, level = item.findtext("cap:event", "", _CAP).casefold().rpartition(" ")
        if event not in _HAZARDS or level not in _LEVELS:
            continue
        try:
            start = datetime.fromisoformat(item.findtext("cap:effective", "", _CAP))
            end = datetime.fromisoformat(item.findtext("cap:expires", "", _CAP))
        except ValueError:
            continue
        if start.utcoffset() is None or end.utcoffset() is None or not (
            start < end and start < tomorrow and now < end
        ):
            continue
        starts = start.astimezone(now.tzinfo)
        ends = end.astimezone(now.tzinfo)
        period = (f"{starts:%H:%M}–{ends:%H:%M}" if starts.date() == ends.date() else
                  f"{starts:%d.%m %H:%M}–{ends:%d.%m %H:%M}")
        text = f"РХМЗ предупреждает: {_HAZARDS[event]}, {_LEVELS[level]} уровень, {period}."
        alerts.append((tuple(_LEVELS).index(level), text))
    return max(alerts)[1] if alerts else None


def parse_forecast(html: str, now: datetime) -> Forecast | None:
    if len(html) > 600_000 or now.utcoffset() is None:
        return None
    page = BeautifulSoup(html, "html.parser")
    rows = [row.find_all(("td", "th"), recursive=False) for row in page.find_all("tr")]
    matches = [cells for cells in rows if cells and cells[0].get_text(strip=True) == "Београд"]
    if len(matches) != 1:
        return None
    cells = matches[0]
    table = cells[0].find_parent("table")
    if table is None:
        return None
    updated = re.search(r"Прогноза ажурирана:\s*(\d{2}\.\d{2}\.)\s*(\d{2}:\d{2})",
                        table.get_text(" ", strip=True))
    if updated is None:
        return None
    try:
        stamp = datetime.strptime(f"{now.year} {updated[1]} {updated[2]}", "%Y %d.%m. %H:%M")
    except ValueError:
        return None
    stamp = stamp.replace(tzinfo=now.tzinfo)
    if stamp > now and now.month == 1 and stamp.month == 12:
        stamp = stamp.replace(year=now.year - 1)
    if not timedelta(0) <= now - stamp <= timedelta(hours=30):
        return None
    for row in table.find_all("tr"):
        offset = 0
        for cell in row.find_all(("td", "th"), recursive=False):
            colspan = cell.get("colspan", "1")
            if not isinstance(colspan, str) or colspan not in {"1", "2", "3", "9", "10"}:
                break
            width = int(colspan)
            day = re.match(r"(\d{2}\.\d{2}\.)\s+", cell.get_text(" ", strip=True))
            if (day and day[1] == now.strftime("%d.%m.") and width in {2, 3}
                    and offset > 0 and offset + width <= len(cells)):
                value = cells[offset + width - 2].get_text(strip=True)
                icon = cells[offset + width - 1].find("img")
                source = icon.get("src", "") if icon is not None else ""
                code = re.search(r"/prognoza/(\d{1,2})\.gif$", str(source))
                if (not re.fullmatch(r"-?\d{1,2}", value) or not -50 <= int(value) <= 60
                        or code is None or not 1 <= int(code[1]) <= len(_CONDITIONS)):
                    return None
                condition = int(code[1])
                return Forecast(f"Днём до {int(value):+d} °C, {_CONDITIONS[condition - 1]}.",
                                condition in {12, 16, 18, 20})
            offset += width
    return None
