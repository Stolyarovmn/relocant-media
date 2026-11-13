"""Точное сопоставление уведомлений с локальными объектами OSM, без сетевых запросов."""

import gzip
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from relocant_media.services.utility_geography import _name
from relocant_media.services.utility_scope import settlement_name_key

type Coordinate = tuple[float, float]
type Rings = tuple[tuple[Coordinate, ...], ...]
type AddressKey = tuple[str, str, str]

_OSM_TYPES = {"n": "node", "w": "way", "r": "relation"}
_OSM_REF = re.compile(r"[nwr][1-9][0-9]{0,12}")
_HOUSE = re.compile(r"[1-9][0-9]{0,4}[а-яђјљњћџ]?")
_SCOPE = re.compile(r",\s*(?:naselje|насеље)\s+", re.IGNORECASE)
_MAX_BYTES = 150 * 1024 * 1024


def _exact(value: str) -> str:
    return _name(unicodedata.normalize("NFC", value))


def _district(value: str) -> str:
    return _exact(value).removeprefix("градска општина ").removeprefix("општина ")


def _street(value: str) -> str:
    return _exact(value).removeprefix("улица ").removeprefix("ул. ")


def _scope(value: str) -> tuple[str, str]:
    values = _SCOPE.split(value, maxsplit=1)
    return _district(values[0]), _exact(values[1]) if len(values) == 2 else ""


def address_key(street: str, number: str, municipality: str) -> AddressKey | None:
    road, district = _street(street), _district(municipality)
    house = _exact(number).replace(" ", "")
    return (district, road, house) if district and road and _HOUSE.fullmatch(house) else None


@dataclass(frozen=True, slots=True)
class AreaPolygon:
    rings: Rings
    osm_url: str


@dataclass(frozen=True, slots=True)
class AddressLocation:
    street: str
    number: str
    municipality: str
    point: Coordinate
    buildings: tuple[AreaPolygon, ...]
    osm_url: str | None
    source_url: str | None = None
    source_id: str | None = None
    snapshot_at: date | None = None


def _ring_contains(ring: tuple[Coordinate, ...], point: Coordinate) -> bool:
    latitude, longitude = point
    inside = False
    for (left_y, left_x), (right_y, right_x) in zip(ring, ring[1:], strict=False):
        if (left_y > latitude) != (right_y > latitude):
            cross = left_x + (latitude - left_y) * (right_x - left_x) / (right_y - left_y)
            if longitude < cross:
                inside = not inside
    return inside


def _within(point: Coordinate, polygons: tuple[AreaPolygon, ...]) -> bool:
    return any(_ring_contains(area.rings[0], point)
               and not any(_ring_contains(hole, point) for hole in area.rings[1:])
               for area in polygons)


class AreaIndex:
    def __init__(
        self, addresses: dict[AddressKey, AddressLocation],
        settlements: dict[tuple[str, str], tuple[AreaPolygon, ...]],
        repeated_settlements: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._addresses = addresses
        self._settlements = settlements
        self._settlement_aliases: dict[tuple[str, str], tuple[AreaPolygon, ...]] = {}
        ambiguous = {(district, settlement_name_key(name))
                     for district, name in repeated_settlements}
        for (district, name), polygons in settlements.items():
            key = district, settlement_name_key(name)
            if key in self._settlement_aliases and self._settlement_aliases[key] != polygons:
                ambiguous.add(key)
            self._settlement_aliases[key] = polygons
        for key in ambiguous:
            self._settlement_aliases.pop(key)
        self._streets: dict[tuple[str, str], list[AddressLocation]] = {}
        for (district, street, _), location in addresses.items():
            self._streets.setdefault((district, street), []).append(location)

    def address(self, street: str, number: str, municipality: str) -> AddressLocation | None:
        district, place = _scope(municipality)
        key = address_key(street, number, district)
        found = self._addresses.get(key) if key else None
        area = self.settlement(place, district)
        # При известной границе проверяем и её. Иначе точная тройка из источника
        # уже уникальна внутри общины, поэтому точку можно показать по адресу.
        if found and (not area or _within(found.point, area)):
            return found
        return None

    def street(self, street: str, municipality: str) -> tuple[AddressLocation, ...]:
        district, place = _scope(municipality)
        found = self._streets.get((district, _street(street)), ())
        if not place:
            return tuple(found)
        area = self.settlement(place, district)
        return tuple(row for row in found if _within(row.point, area)) if area else ()

    def settlement(self, name: str, municipality: str) -> tuple[AreaPolygon, ...]:
        district = _scope(municipality)[0]
        return (self._settlements.get((district, _exact(name)))
                or self._settlement_aliases.get((district, settlement_name_key(name)), ()))

    def settlement_reference(self, name: str, municipality: str) -> Coordinate | None:
        """Ориентир внутри известного поселения; не контур отключения."""
        polygons = self.settlement(name, municipality) or self.settlement(
            f"Naselje {name}", municipality,
        )
        for area in polygons:
            ring = area.rings[0]
            latitudes, longitudes = zip(*ring, strict=True)
            low, high = min(latitudes), max(latitudes)
            west, east = min(longitudes), max(longitudes)
            for x, y in ((.5, .5), (.25, .5), (.75, .5), (.5, .25), (.5, .75)):
                point = low + (high - low) * x, west + (east - west) * y
                if _within(point, (area,)):
                    return point
        return None

    def settlement_contains(self, name: str, municipality: str, point: Coordinate) -> bool:
        return _within(point, self.settlement(name, municipality))


def _point(latitude: Any, longitude: Any) -> Coordinate:
    if (type(latitude) not in (int, float) or type(longitude) not in (int, float)
            or not 44.2 <= latitude <= 45.2 or not 19.8 <= longitude <= 21.1):
        raise ValueError("Координаты вне Белграда")
    return float(latitude), float(longitude)


def _polygon(raw: Any, osm_url: str) -> AreaPolygon:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 13:
        raise ValueError("Недопустимое число колец контура")
    rings = []
    for ring in raw:
        if not isinstance(ring, list) or not 4 <= len(ring) <= 1000:
            raise ValueError("Недопустимое кольцо контура")
        points = tuple(_point(lat, lon) for lat, lon in ring)
        if points[0] != points[-1] or len(set(points)) < 3:
            raise ValueError("Контур не замкнут")
        rings.append(points)
    return AreaPolygon(tuple(rings), osm_url)


def _url(ref: str) -> str:
    if not isinstance(ref, str) or not _OSM_REF.fullmatch(ref):
        raise ValueError("Неизвестный объект OSM")
    return f"https://www.openstreetmap.org/{_OSM_TYPES[ref[0]]}/{ref[1:]}"


def _load(document: Any, fallback: dict[AddressKey, AddressLocation] | None = None) -> AreaIndex:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("Неизвестная версия индекса геометрии")
    buildings = {ref: _polygon(rings, _url(ref)) for ref, rings in document["buildings"].items()}
    addresses = {}
    for street, number, district, lat, lon, ref, building_ref in document["addresses"]:
        key = address_key(street, number, district)
        if key is None or key in addresses:
            raise ValueError("Неоднозначная запись адреса в индексе")
        polygons: tuple[AreaPolygon, ...] = (
            (buildings[building_ref],) if building_ref is not None else ()
        )
        addresses[key] = AddressLocation(street, number, district, _point(lat, lon),
                                         polygons, _url(ref))
    settlements: dict[tuple[str, str], tuple[AreaPolygon, ...]] = {}
    repeated = set()
    for district, names, ref, contours in document["settlements"]:
        polygons = tuple(_polygon(rings, _url(ref)) for rings in contours)
        for name in names:
            lookup = (_district(district), _exact(name))
            if lookup in settlements and settlements[lookup] != polygons:
                repeated.add(lookup)
            settlements[lookup] = polygons
    return AreaIndex({**(fallback or {}), **addresses},
                     {key: value for key, value in settlements.items() if key not in repeated},
                     frozenset(repeated))


def _valid_rgz_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 500 or any(ord(c) <= 32 for c in url):
        return False
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.netloc == "dina.openstreetmap.rs"
            and parts.path.startswith("/ar/opstine/") and parts.path.endswith(".html")
            and not parts.query and not parts.fragment)


def _load_rgz(document: Any) -> dict[AddressKey, AddressLocation]:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("Неизвестная версия адресного индекса RGZ")
    result: dict[AddressKey, AddressLocation] = {}
    repeated = set()
    for report in document["reports"]:
        url = report["url"]
        if not _valid_rgz_url(url):
            raise ValueError("Неизвестный источник RGZ")
        snapshot = date.fromisoformat(report["snapshot_at"])
        for street, number, district, lat, lon, ref in report["addresses"]:
            key = address_key(street, number, district)
            if (key is None or not isinstance(ref, str)
                    or not re.fullmatch(r"[1-9][0-9]{0,11}", ref)):
                raise ValueError("Неизвестный адрес RGZ")
            entry = AddressLocation(street, number, district, _point(lat, lon), (), None,
                                    url, ref, snapshot)
            if key in result and (result[key].point, result[key].source_id) != (entry.point, ref):
                repeated.add(key)
            result[key] = entry
    return {key: value for key, value in result.items() if key not in repeated}


def _read(path: Path) -> Any:
    with gzip.open(path, "rb") as archive:
        payload = archive.read(_MAX_BYTES + 1)
    if len(payload) > _MAX_BYTES:
        raise ValueError("Географический индекс слишком велик")
    return json.loads(payload)


def load_area_index(path: Path) -> AreaIndex:
    """Подхватить контуры OSM и точные адреса из соседней выгрузки RGZ."""
    try:
        fallback = _load_rgz(_read(path.with_name("rgz-addresses.json.gz")))
    except (OSError, EOFError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        fallback = {}
    try:
        return _load(_read(path), fallback)
    except (OSError, EOFError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return AreaIndex(fallback, {})


def within_outage_segment(point: Coordinate, line: tuple[Coordinate, ...]) -> bool:
    """Точка того же адресного ряда лежит вдоль участка между его концами."""
    if len(line) < 2:
        return False
    latitude, longitude = point
    horizontal = math.cos(math.radians(latitude)) * 111_320
    nearby = []
    offset = 0.0
    for (left_lat, left_lon), (right_lat, right_lon) in zip(line, line[1:], strict=False):
        left_x, left_y = (left_lon - longitude) * horizontal, (left_lat - latitude) * 111_320
        dx, dy = (right_lon - left_lon) * horizontal, (right_lat - left_lat) * 111_320
        length = math.hypot(dx, dy)
        if not length:
            continue
        projection = -(left_x * dx + left_y * dy) / (length * length)
        along = min(1.0, max(0.0, projection))
        nearby.append((math.hypot(left_x + along * dx, left_y + along * dy),
                       offset + along * length))
        offset += length
    if not nearby:
        return False
    distance, along = min(nearby)
    return distance <= 40 and 4 <= along <= offset - 4
