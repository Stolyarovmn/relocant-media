"""Build a static, attributed address index from a complete Serbia OSM extract."""

import argparse
import gzip
import hashlib
import io
import json
import re
from collections.abc import Mapping
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from relocant_media.services.utility_geography import _municipality

_CITY_RELATION = 1677007
# A prefilter for memory use. The actual district and municipal polygons decide inclusion.
_ENVELOPE = (19.8, 44.2, 21.1, 45.2)
_HOUSE_NUMBER = re.compile(r"[0-9]{1,5}[A-Za-zČĆĐŠŽčćđšžА-Яа-яЂђЈјЉљЊњЋћЏџ]?")
_MUNICIPALITY_NAMES = (
    "Barajevo", "Voždovac", "Vračar", "Grocka", "Zvezdara", "Zemun", "Lazarevac",
    "Mladenovac", "Novi Beograd", "Obrenovac", "Palilula", "Rakovica", "Savski venac",
    "Sopot", "Stari grad", "Surčin", "Čukarica",
)
_MUNICIPALITIES = {_municipality({"municipality": name}): name for name in _MUNICIPALITY_NAMES}
_SOURCE = {
    "name": "© OpenStreetMap contributors",
    "url": "https://download.geofabrik.de/europe/serbia.html",
    "license": "ODbL-1.0",
    "license_url": "https://www.openstreetmap.org/copyright",
}
_ROAD_TYPES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
    "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
    "residential", "living_street", "service", "road", "pedestrian", "track",
    "footway", "cycleway", "path", "steps",
}

type Address = tuple[str, str, str, float, float]
type Candidate = tuple[str, str, float, float]
type RoadNode = tuple[int, float, float]
type Road = tuple[int, str, list[RoadNode]]
type Boundary = dict[str, list[int]]


def _address(tags: Mapping[str, str]) -> tuple[str, str] | None:
    street = " ".join((tags.get("addr:street") or tags.get("addr:place") or "").split())
    number = (tags.get("addr:housenumber") or "").strip()
    if (not 1 <= len(street) <= 160 or not _HOUSE_NUMBER.fullmatch(number)
            or tags.get("addr:interpolation")):
        return None
    return street, number


def _inside_envelope(longitude: float, latitude: float) -> bool:
    west, south, east, north = _ENVELOPE
    return west <= longitude <= east and south <= latitude <= north


def _boundary_name(tags: Mapping[str, str]) -> str | None:
    if tags.get("boundary") != "administrative" or tags.get("admin_level") != "8":
        return None
    name = tags.get("name:sr-Latn") or tags.get("name", "")
    return _MUNICIPALITIES.get(_municipality({"municipality": name}))


def _named_road(way: Any) -> Road | None:
    name = (way.tags.get("name") or "").strip()
    if (not 1 <= len(name) <= 160 or way.tags.get("highway") not in _ROAD_TYPES
            or way.tags.get("area") == "yes" or not 2 <= len(way.nodes) <= 2000):
        return None
    nodes = []
    for node in way.nodes:
        location = node.location
        if not location.valid() or not _inside_envelope(location.lon, location.lat):
            return None
        nodes.append((node.ref, location.lat, location.lon))
    return way.id, name, nodes


def _area(members: Boundary, ways: Mapping[int, Any], shapely: Any) -> Any:
    parts = {}
    for role, references in members.items():
        if not references:
            continue
        if not all(reference in ways for reference in references):
            return None
        rings, cuts, dangles, invalid = shapely.polygonize_full([
            ways[reference] for reference in references
        ])
        if rings.is_empty or not (cuts.is_empty and dangles.is_empty and invalid.is_empty):
            return None
        parts[role] = shapely.union_all(rings.geoms)
    outer = parts.get("outer")
    if outer is None:
        return None
    return outer.difference(parts["inner"]) if "inner" in parts else outer


def _city_boundaries(input_path: Path, osmium: Any, shapely: Any) -> tuple[Any, list[Any]]:
    relations: list[tuple[str | None, Boundary]] = []
    required_ways: set[int] = set()
    objects = osmium.FileProcessor(str(input_path), osmium.osm.RELATION).with_filter(
        osmium.filter.TagFilter(("boundary", "administrative"))
    )
    for relation in objects:
        district = relation.id == _CITY_RELATION
        name = None if district else _boundary_name(relation.tags)
        if not district and name is None:
            continue
        parts: Boundary = {"outer": [], "inner": []}
        for member in relation.members:
            if member.type == "w" and member.role in ("outer", "inner", ""):
                parts[member.role or "outer"].append(member.ref)
        if parts["outer"]:
            relations.append((name, parts))
            required_ways.update(parts["outer"] + parts["inner"])

    ways: dict[int, Any] = {}
    objects = (osmium.FileProcessor(str(input_path)).with_locations()
               .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
               .with_filter(osmium.filter.IdFilter(required_ways)))
    for way in objects:
        if len(way.nodes) >= 2 and all(node.location.valid() for node in way.nodes):
            ways[way.id] = shapely.LineString([(node.lon, node.lat) for node in way.nodes])

    city = None
    boundaries: dict[str, list[Any]] = {}
    for name, parts in relations:
        polygon = _area(parts, ways, shapely)
        if polygon is None or not polygon.is_valid:
            continue
        if name is None:
            city = polygon
        else:
            boundaries.setdefault(name, []).append(polygon)

    if city is None:
        raise ValueError("В выгрузке нет корректной границы Белграда")
    west, south, east, north = city.bounds
    if not (_inside_envelope(west, south) and _inside_envelope(east, north)):
        raise ValueError("Граница Белграда вышла за рамки предварительного фильтра")
    shapely.prepare(city)
    municipal_polygons = []
    for name in _MUNICIPALITY_NAMES:
        matching = [polygon for polygon in boundaries.get(name, [])
                    if city.covers(polygon.representative_point())]
        if len(matching) != 1:
            raise ValueError(f"В выгрузке нет однозначной границы общины {name}")
        municipal_polygons.append(matching[0])
    return city, municipal_polygons


def build_indexes(
    input_path: Path, *, include_roads: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Use tagged addresses and named ways within the 17 Belgrade municipalities."""
    # The normal editorial runner has no GIS dependencies. Only this offline builder uses them.
    osmium = import_module("osmium")
    shapely = import_module("shapely")
    with osmium.io.Reader(str(input_path), osmium.osm.NOTHING) as reader:
        snapshot = reader.header().get("osmosis_replication_timestamp")
    datetime.fromisoformat(snapshot)

    city, municipal_polygons = _city_boundaries(input_path, osmium, shapely)
    spatial_index = shapely.STRtree(municipal_polygons)
    candidates: list[Candidate] = []
    roads: list[Road] = []
    keys = ["addr:housenumber"]
    if include_roads:
        keys.append("highway")
    objects = (osmium.FileProcessor(str(input_path)).with_locations()
               .with_filter(osmium.filter.KeyFilter(*keys)))
    for item in objects:
        if isinstance(item, osmium.osm.Node):
            location = item.location
            if location.valid() and _inside_envelope(location.lon, location.lat):
                address = _address(item.tags)
                if address:
                    candidates.append((*address, location.lat, location.lon))
            continue
        if not isinstance(item, osmium.osm.Way):
            continue
        if include_roads:
            road = _named_road(item)
            if road is not None:
                roads.append(road)
        address = _address(item.tags)
        if not address or not item.is_closed() or not 4 <= len(item.nodes) <= 2000:
            continue
        if not all(node.location.valid() and _inside_envelope(node.lon, node.lat)
                   for node in item.nodes):
            continue
        polygon = shapely.Polygon([(node.lon, node.lat) for node in item.nodes])
        if polygon.is_valid:
            point = polygon.representative_point()
            candidates.append((*address, point.y, point.x))

    addresses: set[Address] = set()
    for street, number, latitude, longitude in candidates:
        point = shapely.Point(longitude, latitude)
        matches = spatial_index.query(point, predicate="covered_by")
        if len(matches) == 1 and city.covers(point):
            addresses.add((
                street, number, _MUNICIPALITY_NAMES[int(matches[0])],
                round(latitude, 6), round(longitude, 6),
            ))
    if {row[2] for row in addresses} != set(_MUNICIPALITY_NAMES):
        raise ValueError("В адресном индексе отсутствуют данные хотя бы одной общины")
    with input_path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    metadata = {**_SOURCE, "snapshot_at": snapshot, "extract_sha256": checksum}
    index = {
        "version": 1,
        "source": metadata,
        "addresses": sorted(addresses, key=lambda row: (row[0].casefold(), *row)),
    }
    if not include_roads:
        return index, None

    municipal_roads: dict[str, list[Road]] = {name: [] for name in _MUNICIPALITY_NAMES}
    for road in sorted(roads):
        line = shapely.LineString([(longitude, latitude) for _, latitude, longitude in road[2]])
        matches = spatial_index.query(line, predicate="covered_by")
        if len(matches) == 1 and city.covers(line):
            municipal_roads[_MUNICIPALITY_NAMES[int(matches[0])]].append(road)
    if not all(municipal_roads.values()):
        raise ValueError("В дорожном индексе отсутствуют данные хотя бы одной общины")
    return index, {"version": 1, "source": metadata, "municipalities": municipal_roads}


def _encoded_gzip(data: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as output:
        output.write(data)
    return buffer.getvalue()


def _write_index(index: dict[str, Any], output: Path) -> int:
    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode()
    if output.suffix == ".gz":
        payload = _encoded_gzip(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".new")
    temporary.write_bytes(payload)
    temporary.replace(output)
    return len(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Собрать индекс адресов и дорог из OSM PBF")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--roads-output", type=Path)
    args = parser.parse_args()

    index, roads = build_indexes(args.input, include_roads=args.roads_output is not None)
    size = _write_index(index, args.output)
    print(f"Адресов: {len(index['addresses'])}; размер: {size} байт")
    if roads is not None:
        size = _write_index(roads, args.roads_output)
        count = sum(len(ways) for ways in roads["municipalities"].values())
        print(f"Дорожных объектов: {count}; размер: {size} байт")


if __name__ == "__main__":
    main()
