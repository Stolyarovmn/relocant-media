"""Геометрия официального notice: только объявленный JSON, без выполнения JavaScript."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from relocant_media.ai.models import EvidenceDocument

MAX_HTML_BYTES = 2_000_000
MAX_JSON_BYTES = 1_000_000
MAX_LINE_POINTS = 1_000
_HOSTS = {"bgprevoz.rs", "www.bgprevoz.rs"}
_NOTICE_PATH = re.compile(r"/linije/aktuelne-izmene/([1-9][0-9]{0,11})\Z")
_DECLARATION = re.compile(r"\A\s*const\s+izmena\s*=\s*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,20})?\Z")
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)"
    r"|opacity\s*:\s*0(?:\.0*)?)(?:\s*!important)?\s*(?:;|$)", re.I,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate transport map JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite transport map JSON value: {value}")


def _decoder() -> json.JSONDecoder:
    return json.JSONDecoder(
        parse_float=Decimal, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
    )


def _notice_id(document: EvidenceDocument) -> int | None:
    if document.source_id != "bgprevoz_changes":
        return None
    url = document.url
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in _HOSTS:
        return None
    if "?" in url or "#" in url:
        return None
    match = _NOTICE_PATH.fullmatch(parsed.path)
    return int(match[1]) if match else None


def _visible(tag: Tag) -> bool:
    for node in (tag, *tag.parents):
        if not isinstance(node, Tag):
            continue
        if node.name in {"template", "noscript", "script"}:
            return False
        if node.has_attr("hidden") or node.has_attr("inert"):
            return False
        if str(node.get("aria-hidden", "")).lower() == "true":
            return False
        classes = node.get_attribute_list("class")
        if any(str(value).split(":")[-1] in {"hidden", "invisible", "collapse"}
               for value in classes):
            return False
        if _HIDDEN_STYLE.search(str(node.get("style", ""))):
            return False
    return True


def _coordinate(value: object, limit: int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("invalid transport map coordinate type")
    text = str(value)
    if not _NUMBER.fullmatch(text):
        raise ValueError("invalid transport map decimal")
    number = Decimal(text)
    if not number.is_finite() or abs(number) > limit:
        raise ValueError("transport map coordinate outside range")
    if number == 0:
        return "0"
    canonical = format(number, "f")
    return canonical.rstrip("0").rstrip(".") if "." in canonical else canonical


def _line(value: object) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("invalid transport map line encoding")
    zone = _decoder().decode(value)
    if not isinstance(zone, dict) or set(zone) != {"coordinates"}:
        raise ValueError("unknown transport map line format")
    points = zone["coordinates"]
    if not isinstance(points, list) or not 2 <= len(points) <= MAX_LINE_POINTS:
        raise ValueError("invalid transport map line size")
    result: list[list[str]] = []
    for point in points:
        if not isinstance(point, dict) or set(point) != {"lat", "lng"}:
            raise ValueError("invalid transport map line point")
        result.append([_coordinate(point["lat"], 90), _coordinate(point["lng"], 180)])
    if len({tuple(point) for point in result}) < 2:
        raise ValueError("transport map line has no length")
    return result


def extract_transport_map(document: EvidenceDocument, html: str) -> dict[str, Any] | None:
    """Возвращаем точную точку и исходную полилинию либо None при любой неоднозначности.

    Вызывающий код отвечает за привязку HTML к проверенному snapshot документа и его
    live-проверку. Здесь нет HTTP, геокодирования, полигонов и синтеза отсутствующих линий.
    Меняющиеся даты/маршруты/другие notices не входят в возвращаемую геометрию.
    """
    try:
        notice_id = _notice_id(document)
        if notice_id is None or len(html.encode("utf-8")) > MAX_HTML_BYTES:
            return None
        soup = BeautifulSoup(html, "html.parser")
        containers = soup.find_all(id="gmap-r")
        if len(containers) != 1 or containers[0].name != "div" or not _visible(containers[0]):
            return None
        candidates: list[str] = []
        for script in soup.find_all("script"):
            # Не читаем внешние скрипты, JSON-LD, шаблоны и строки внутри другого кода.
            if script.has_attr("src") or script.get("type", "") not in (
                "", "text/javascript", "application/javascript",
            ):
                continue
            if any(parent.name in {"template", "noscript"} for parent in script.parents):
                continue
            text = script.get_text()
            match = _DECLARATION.match(text)
            if match:
                candidates.append(text[match.end():])
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        data, end = _decoder().raw_decode(candidate[:MAX_JSON_BYTES + 1])
        if len(candidate[:end].encode("utf-8")) > MAX_JSON_BYTES:
            return None
        if not candidate[end:].lstrip().startswith(";") or not isinstance(data, dict):
            return None
        if type(data.get("id")) is not int or data["id"] != notice_id:
            return None
        if type(data.get("mapa_izmene")) is not int or data["mapa_izmene"] != 1:
            return None
        return {
            "latitude": _coordinate(data.get("lokacija_sirina"), 90),
            "longitude": _coordinate(data.get("lokacija_duzina"), 180),
            "line": _line(data.get("zona_radova")),
        }
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return None
