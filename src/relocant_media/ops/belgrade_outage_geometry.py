"""Собрать точные адреса, контуры зданий и границы поселений из OSM PBF."""

import argparse
import hashlib
from collections import defaultdict
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Any

from relocant_media.ops.belgrade_address_index import (
    _MUNICIPALITY_NAMES,
    _SOURCE,
    Boundary,
    _address,
    _area,
    _city_boundaries,
    _inside_envelope,
    _write_index,
)
from relocant_media.services.local_outage_geometry import address_key

type Position = tuple[float, float]
type Rings = tuple[tuple[Position, ...], ...]
type AddressObject = tuple[str, str, str, Any, str | None]
type AddressRow = tuple[str, str, str, float, float, str, str | None]


def _closed_polygon(way: Any, shapely: Any) -> Any:
    if not way.is_closed() or not 4 <= len(way.nodes) <= 2000:
        return None
    if not all(node.location.valid() and _inside_envelope(node.lon, node.lat)
               for node in way.nodes):
        return None
    polygon = shapely.Polygon([(node.lon, node.lat) for node in way.nodes])
    return polygon if polygon.is_valid and not polygon.is_empty else None


def _rings(polygon: Any, *, limit: int) -> Rings | None:
    if len(polygon.interiors) > 12:
        return None
    result = []
    for ring in (polygon.exterior, *polygon.interiors):
        points = tuple((round(lat, 6), round(lon, 6)) for lon, lat in ring.coords)
        if not 4 <= len(points) <= limit or len(set(points)) < 3:
            return None
        result.append(points)
    return tuple(result)


def _municipality(geometry: Any, city: Any, tree: Any) -> str | None:
    if not city.covers(geometry):
        return None
    matches = tree.query(geometry, predicate="covered_by")
    return _MUNICIPALITY_NAMES[int(matches[0])] if len(matches) == 1 else None


def _relations(input_path: Path, osmium: Any) -> tuple[
    list[tuple[int, tuple[str, ...], Boundary]], dict[int, tuple[Boundary, tuple[str, str] | None]],
]:
    settlements = []
    buildings = {}
    processor = osmium.FileProcessor(str(input_path), osmium.osm.RELATION)
    for relation in processor:
        tags = relation.tags
        settlement = (tags.get("boundary") == "administrative"
                      and tags.get("admin_level") in {"9", "10"}
                      and tags.get("type") in {"boundary", "multipolygon"})
        building = (tags.get("type") == "multipolygon"
                    and tags.get("building") not in {None, "no"})
        if not settlement and not building:
            continue
        members: Boundary = {"outer": [], "inner": []}
        for member in relation.members:
            if member.type == "w" and member.role in {"", "outer", "inner"}:
                members[member.role or "outer"].append(member.ref)
        if not members["outer"]:
            continue
        if building:
            buildings[relation.id] = members, _address(tags)
        elif settlement:
            names = tuple(dict.fromkeys(value for tag in (
                "name", "name:sr-Latn", "short_name", "short_name:sr-Latn",
            ) if (value := tags.get(tag))))
            if names:
                settlements.append((relation.id, names, members))
    return settlements, buildings


def _member_lines(
    input_path: Path, osmium: Any, shapely: Any, references: set[int],
) -> dict[int, Any]:
    if not references:
        return {}
    processor = (osmium.FileProcessor(str(input_path)).with_locations()
                 .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
                 .with_filter(osmium.filter.IdFilter(references)))
    result = {}
    for way in processor:
        if len(way.nodes) >= 2 and all(node.location.valid() for node in way.nodes):
            if not any(_inside_envelope(node.lon, node.lat) for node in way.nodes):
                continue
            result[way.id] = shapely.LineString([(node.lon, node.lat) for node in way.nodes])
    return result


def _settlements(
    relations: list[tuple[int, tuple[str, ...], Boundary]], lines: dict[int, Any],
    city: Any, tree: Any, shapely: Any,
) -> list[tuple[str, tuple[str, ...], str, tuple[Rings, ...]]]:
    rows = []
    city_buffer = city.buffer(0.000003)
    for relation_id, names, parts in sorted(relations):
        polygon = _area(parts, lines, shapely)
        if polygon is None or not polygon.is_valid:
            continue
        # Совпадающие с границей общины узлы иногда различаются на сантиметры.
        municipality = _municipality(polygon, city_buffer, tree)
        if municipality is None:
            municipality = _municipality(polygon.representative_point(), city, tree)
            if municipality is None:
                continue
            district = tree.geometries[_MUNICIPALITY_NAMES.index(municipality)]
            if not district.buffer(0.000003).covers(polygon):
                continue
        geometry = polygon.simplify(0.000018, preserve_topology=True)
        polygons = (geometry,) if geometry.geom_type == "Polygon" else tuple(geometry.geoms)
        contours = tuple(_rings(part, limit=1000) for part in polygons)
        if contours and all(contour is not None for contour in contours):
            rows.append((municipality, names, f"r{relation_id}",
                         tuple(contour for contour in contours if contour is not None)))
    return rows


def _building_geometry(
    input_path: Path, osmium: Any, shapely: Any, city: Any, tree: Any,
    relations: dict[int, tuple[Boundary, tuple[str, str] | None]], lines: dict[int, Any],
) -> tuple[list[AddressObject], dict[str, Any]]:
    addresses: list[AddressObject] = []
    buildings = {}
    members = set()
    for relation_id, (parts, address) in relations.items():
        polygon = _area(parts, lines, shapely)
        if polygon is None or polygon.geom_type != "Polygon" or not polygon.is_valid:
            continue
        if _municipality(polygon, city, tree) is None:
            continue
        ref = f"r{relation_id}"
        buildings[ref] = polygon
        members.update(parts["outer"])
        if address:
            addresses.append((*address, ref, polygon.representative_point(), ref))

    processor = (osmium.FileProcessor(str(input_path)).with_locations()
                 .with_filter(osmium.filter.KeyFilter("building", "addr:housenumber")))
    for item in processor:
        if isinstance(item, osmium.osm.Node):
            location = item.location
            if location.valid() and _inside_envelope(location.lon, location.lat):
                if address := _address(item.tags):
                    point = shapely.Point(location.lon, location.lat)
                    if city.covers(point):
                        addresses.append((*address, f"n{item.id}", point, None))
            continue
        if not isinstance(item, osmium.osm.Way):
            continue
        address = _address(item.tags)
        building = item.tags.get("building") not in {None, "no"} and item.id not in members
        if not address and not building:
            continue
        polygon = _closed_polygon(item, shapely)
        if polygon is None:
            continue
        point = polygon.representative_point()
        if not city.covers(point):
            continue
        ref = f"w{item.id}"
        if building and _municipality(polygon, city, tree) is not None:
            buildings[ref] = polygon
        if address:
            addresses.append((*address, ref, point, ref if ref in buildings else None))
    return addresses, buildings


def _address_rows(
    addresses: list[AddressObject], buildings: dict[str, Any], city: Any, tree: Any, shapely: Any,
) -> tuple[list[AddressRow], dict[str, Rings]]:
    building_refs = list(buildings)
    building_index = shapely.STRtree([buildings[ref] for ref in building_refs])
    grouped: dict[tuple[str, str, str], set[AddressRow]] = defaultdict(set)
    building_keys: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for street, number, ref, point, building in addresses:
        municipality = _municipality(point, city, tree)
        if municipality is None:
            continue
        if building is None:
            matches = building_index.query(point, predicate="covered_by")
            if len(matches) == 1:
                building = building_refs[int(matches[0])]
        row = (street, number, municipality, round(point.y, 6), round(point.x, 6), ref, building)
        key = address_key(street, number, municipality)
        if key is None:
            continue
        grouped[key].add(row)
        if building is not None:
            building_keys[building].add(key)

    rows = []
    footprints = {}
    for candidates in grouped.values():
        ordered = sorted(candidates, key=lambda row: (row[6] != row[5], *row[:6]))
        if len(ordered) > 1:
            # Точка адреса и контур того же дома часто отмечены двумя объектами OSM.
            first = ordered[0]
            if not all(row[3:5] == first[3:5] or row[6] and row[6] == first[6]
                       for row in ordered[1:]):
                continue
        chosen = ordered[0]
        building = chosen[6]
        if building and len(building_keys[building]) == 1:
            contour = _rings(buildings[building], limit=250)
            if contour is not None:
                footprints[building] = contour
                rows.append(chosen)
                continue
        rows.append((*chosen[:6], None))
    return sorted(rows), dict(sorted(footprints.items()))


def build_outage_index(input_path: Path) -> dict[str, Any]:
    """Строить офлайн, используя один подписанный по SHA-256 снимок OSM."""
    osmium = import_module("osmium")
    shapely = import_module("shapely")
    with osmium.io.Reader(str(input_path), osmium.osm.NOTHING) as reader:
        snapshot = reader.header().get("osmosis_replication_timestamp")
    datetime.fromisoformat(snapshot)
    city, districts = _city_boundaries(input_path, osmium, shapely)
    tree = shapely.STRtree(districts)
    settlements, building_relations = _relations(input_path, osmium)
    references = {
        ref for parts in [*(parts for _, _, parts in settlements),
                          *(parts for parts, _ in building_relations.values())]
        for ways in parts.values() for ref in ways
    }
    lines = _member_lines(input_path, osmium, shapely, references)
    settlement_rows = _settlements(settlements, lines, city, tree, shapely)
    addresses, buildings = _building_geometry(
        input_path, osmium, shapely, city, tree, building_relations, lines,
    )
    rows, footprints = _address_rows(addresses, buildings, city, tree, shapely)
    if ({row[2] for row in rows} != set(_MUNICIPALITY_NAMES)
            or not footprints or not settlement_rows):
        raise ValueError("Неполный географический индекс Белграда")
    with input_path.open("rb") as source:
        checksum = hashlib.file_digest(source, "sha256").hexdigest()
    metadata = {**_SOURCE, "snapshot_at": snapshot, "extract_sha256": checksum}
    return {"version": 1, "source": metadata, "addresses": rows, "buildings": footprints,
            "settlements": settlement_rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    index = build_outage_index(args.input)
    size = _write_index(index, args.output)
    print(f"Адресов: {len(index['addresses'])}, контуров зданий: {len(index['buildings'])}, "
          f"границ поселений: {len(index['settlements'])}, размер: {size} байт")


if __name__ == "__main__":
    main()
