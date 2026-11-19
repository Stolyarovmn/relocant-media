"""Снимок трасс из публичной карты, встроенной в страницу организатора."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from relocant_media.domain.event_programs import MARATHON_PAGE_URL
from relocant_media.services.exhibition_map_point import MAX_HTML_BYTES

_MAP_ID = "1wLdBTheHVqeu9iZ88dKCzF9QcnQ"
MARATHON_KML_URL = f"https://www.google.com/maps/d/kml?mid={_MAP_ID}&forcekml=1"
_EMBED_URL = f"https://www.google.com/maps/d/embed?mid={_MAP_ID}"
_SCRIPT_ID = "relocant-halfmarathon-kml"
_KML_MAX_BYTES = 300_000
_KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
_COURSES = (
    ("Sava osiguranje Beogradski polumaraton - 10K", "10K", "10 км"),
    ("Sava osiguranje Beogradski polumaraton - 21K", "POLUMARATON", "21 км"),
)
_DATE = re.compile(r"\bdatum održavanja:\s*nedelja,\s*22\.\s+novembar\s+2026\b")
_START = re.compile(r"\bstart trke:\s*10:00(?!\d)")
_HOURS = re.compile(r"\bvreme događaja:\s*08:00\s*[-–—]\s*14:00(?!\d)")
_ORGANIZER = re.compile(r"\borganizator:\s*beogradski maraton\b")
_DISTANCES = re.compile(r"\bdiscipline:\s*10k\s+i\s+21k\b")
_CANCELLED = re.compile(r"\b(?:trka je (?:otkazana|odložena)|polumaraton je odložen)\b")


def _official_schedule(text: str) -> bool:
    text = " ".join(text.casefold().split())
    return all(p.search(text) for p in (_DATE, _START, _HOURS, _ORGANIZER, _DISTANCES)) and (
        _CANCELLED.search(text) is None
    )


def _official_page(html: str) -> BeautifulSoup:
    if len(html.encode()) > MAX_HTML_BYTES:
        raise ValueError("Снимок организатора слишком большой")
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.select('head link[rel="canonical"][href]')
    if canonical and (len(canonical) != 1 or canonical[0].get("href") != MARATHON_PAGE_URL):
        raise ValueError("Изменился канонический URL организатора")
    official_maps = 0
    for node in soup.select("iframe[src]"):
        src = node.get("src")
        if not isinstance(src, str):
            continue
        parts = urlsplit(src)
        if (
            parts.scheme == "https"
            and parts.netloc == "www.google.com"
            and parts.path == "/maps/d/embed"
            and not parts.fragment
            and parse_qs(parts.query).get("mid") == [_MAP_ID]
        ):
            official_maps += 1
    if official_maps != 1 or not _official_schedule(soup.get_text(" ", strip=True)):
        raise ValueError("Официальная карта или расписание полумарафона изменились")
    return soup


def _coordinates(text: str, *, minimum: int, maximum: int) -> list[list[float]]:
    points: list[list[float]] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) not in {2, 3}:
            raise ValueError("Некорректная координата KML")
        lon, lat = map(float, parts[:2])
        if not (19.8 <= lon <= 21.1 and 44.2 <= lat <= 45.2):
            raise ValueError("Координата трассы вне области Белграда")
        points.append([lon, lat])
    if not minimum <= len(points) <= maximum:
        raise ValueError("Некорректное количество координат KML")
    return points


def _child(parent: ElementTree.Element, path: str, name: str) -> ElementTree.Element:
    matching = [item for item in parent.findall(path, _KML_NS)
                if item.findtext("k:name", namespaces=_KML_NS) == name]
    if len(matching) != 1:
        raise ValueError("Карта не содержит единственного ожидаемого слоя или объекта")
    return matching[0]


def _geometry(kml: str) -> tuple[list[float], list[dict[str, Any]]]:
    if len(kml.encode()) > _KML_MAX_BYTES or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", kml, re.I):
        raise ValueError("Недопустимый формат KML полумарафона")
    try:
        root = ElementTree.fromstring(kml)
    except ElementTree.ParseError as error:
        raise ValueError("Некорректный KML полумарафона") from error
    documents = root.findall("k:Document", _KML_NS)
    if len(documents) != 1:
        raise ValueError("Ожидалась одна официальная карта")
    document = documents[0]
    services = _child(document, "k:Folder", "Sadržaji")
    point = _child(services, "k:Placemark", "Start")
    starts = point.findall("k:Point/k:coordinates", _KML_NS)
    if len(starts) != 1:
        raise ValueError("Карта не содержит точной точки старта")
    start = _coordinates(starts[0].text or "", minimum=1, maximum=1)[0]
    courses = []
    for folder, feature, label in _COURSES:
        layer = _child(document, "k:Folder", folder)
        trace = _child(layer, "k:Placemark", feature)
        lines = trace.findall("k:LineString/k:coordinates", _KML_NS)
        if len(lines) != 1:
            raise ValueError("Карта не содержит ожидаемую линию трассы")
        coordinates = _coordinates(lines[0].text or "", minimum=2, maximum=4096)
        if (max(abs(a - b) for a, b in zip(start, coordinates[0], strict=True)) > 0.001
                or len({tuple(p) for p in coordinates}) < 2):
            raise ValueError("Трасса не связана с заявленным стартом")
        courses.append({"name": label, "coordinates": coordinates})
    if courses[0]["coordinates"] == courses[1]["coordinates"]:
        raise ValueError("Опубликована одинаковая трасса для обеих дистанций")
    return start, courses


def attach_marathon_kml(html: str, kml: str) -> str:
    """Собрать сырой HTML и проверенную выгрузку карты в один канонический снимок."""
    soup = _official_page(html)
    if soup.select(f"#{_SCRIPT_ID}"):
        raise ValueError("Страница содержит повторную редакционную выгрузку карты")
    _geometry(kml)
    # JSON сохраняет исходные байты координат. Экранируем '<', чтобы содержимое
    # официальной карты оставалось данными даже при появлении строки '</script>'.
    payload = json.dumps(kml, ensure_ascii=False).replace("<", "\\u003c")
    snapshot = html + f'\n<script id="{_SCRIPT_ID}" type="application/json">{payload}</script>'
    if len(snapshot.encode()) > MAX_HTML_BYTES:
        raise ValueError("Объединённый снимок карты слишком большой")
    return snapshot


def marathon_event_facts(body: str, source_html: str) -> dict[str, Any] | None:
    """Повторно вывести план из проверенного HTML и карты того же снимка."""
    if not _official_schedule(body):
        return None
    try:
        soup = _official_page(source_html)
        scripts = soup.select(f'script#{_SCRIPT_ID}[type="application/json"]')
        if len(scripts) != 1 or scripts[0].string is None:
            return None
        kml = json.loads(str(scripts[0].string))
        if not isinstance(kml, str):
            return None
        start, courses = _geometry(kml)
    except ValueError:
        return None
    return {
        "event_name": "7. Sava osiguranje Beogradski polumaraton",
        "venue": {"name": "Старт полумарафона", "address": "Bulevar Zorana Đinđića, Beograd",
                  "latitude": start[1], "longitude": start[0]},
        "starts_at": datetime(2026, 11, 22, 10).isoformat(),
        "ends_at": datetime(2026, 11, 22, 14).isoformat(),
        "time_basis": "source_belgrade_halfmarathon_schedule",
        "schedule_source_url": MARATHON_PAGE_URL,
        "coordinate_source_url": _EMBED_URL,
        "venue_source_url": MARATHON_PAGE_URL,
        "courses": courses,
    }
