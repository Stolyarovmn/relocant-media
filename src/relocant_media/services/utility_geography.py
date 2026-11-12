"""Ограниченное сопоставление объявлений Белграда с существующими объектами OSM.

Адреса не расширяются из диапазонов. Участок дороги допустим только между
единственными общими узлами с указанными поперечными улицами и по единственному
пути самой улицы. Сбой/неполные данные дают пустую геометрию, без повторов запроса.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
PHOTON_URL = "https://photon.komoot.io/api"
OSM_ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"
MAX_INPUTS = 8
MAX_RESPONSE_BYTES = 512 * 1024
MAX_ELEMENTS = 1000
MAX_ROAD_NODES = 2000
REQUEST_TIMEOUT = 12.0
TOTAL_TIMEOUT = 30.0
_BBOX = "44.2,19.8,45.2,21.1"
_USER_AGENT = "RelocantMedia/1.0 (+https://t.me/eduvbelgrad)"
_PHOTON_LIMIT = 6
_POWER_QUERY_LIMIT = 4
_MUNICIPALITIES = {
    "барајево",
    "вождовац",
    "врачар",
    "гроцка",
    "звездара",
    "земун",
    "лазаревац",
    "младеновац",
    "нови београд",
    "обреновац",
    "палилула",
    "раковица",
    "савски венац",
    "сопот",
    "стари град",
    "сурчин",
    "чукарица",
}
_LATIN = str.maketrans(
    dict(
        zip(
            "abcčćdđefghijk lmnoprsštuvzž".replace(" ", ""),
            "абцчћдђефгхијклмнопрсштувзж",
            strict=True,
        )
    )
)


def _name(value: str) -> str:
    value = " ".join(value.casefold().split())
    for latin, cyrillic in (("dž", "џ"), ("lj", "љ"), ("nj", "њ")):
        value = value.replace(latin, cyrillic)
    return value.translate(_LATIN)


def _valid_input(value: Any, *, segment: bool) -> dict[str, str] | None:
    keys = (
        {"municipality", "street", "start", "end"}
        if segment
        else {
            "municipality",
            "street",
            "number",
        }
    )
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or any(not isinstance(v, str) or not 1 <= len(v) <= 160 for v in value.values())
        or any(any(ord(c) < 32 for c in v) for v in value.values())
    ):
        return None
    municipality = _name(value["municipality"])
    for prefix in ("градска општина ", "општина "):
        municipality = municipality.removeprefix(prefix)
    if municipality not in _MUNICIPALITIES:
        return None
    if not segment and not re.fullmatch(r"[0-9]{1,5}[а-яђјљњћџ]?", _name(value["number"])):
        return None
    if any(not _name(value[key]) for key in keys):
        return None
    return dict(value)


def _municipality(value: dict[str, str]) -> str:
    name = _name(value["municipality"])
    return name.removeprefix("градска општина ").removeprefix("општина ")


def _match(key: str, values: list[str]) -> str:
    pattern = "^(" + "|".join(re.escape(_name(value)) for value in sorted(set(values))) + ")$"
    return f"[{json.dumps(key)}~{json.dumps(pattern, ensure_ascii=False)},i]"


def _cross_street_names(value: str) -> set[str]:
    """Две узкие падежные формы BVK; наличие обеих улиц считается неоднозначным."""
    name = _name(value)
    forms = {name}
    if name.endswith(("ске", "чке")):
        forms.add(name[:-1] + "а")
    return forms


def _query(
    municipality: str, addresses: list[dict[str, str]], segments: list[dict[str, str]]
) -> str:
    boundary = _match("name", ["градска општина " + municipality, municipality])
    query = (
        f"[out:json][timeout:8][maxsize:33554432];"
        f'rel["boundary"="administrative"]["admin_level"="8"]{boundary}({_BBOX})->.m;'
        ".m out tags;.m map_to_area->.a;"
    )
    if addresses:
        parts: list[str] = []
        for address in addresses:
            street = _match("addr:street", [address["street"]])
            number = _match("addr:housenumber", [address["number"]])
            # Буквенные номера OSM хранятся на латинице; оба написания проверяются
            # локально тем же строгим нормализатором, без фонетического сходства.
            if re.search(r"[^0-9]", address["number"]):
                number = '["addr:housenumber"]'
            parts.extend(f"{kind}(area.a){street}{number};" for kind in ("node", "way"))
        query += (
            "(" + "".join(parts) + ")->.ad;.ad out body center;"
            "way.ad->.aw;node(w.aw)(area.a);out skel;"
        )
    if segments:
        names = [
            name
            for segment in segments
            for key in ("street", "start", "end")
            for name in (
                {_name(segment[key])} if key == "street" else _cross_street_names(segment[key])
            )
        ]
        query += (
            f'way(area.a)["highway"]{_match("name", names)}->.roads;'
            ".roads out body geom;node(w.roads)(area.a);out skel;"
        )
    return query


def _coordinate(value: Any, *, latitude: bool) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError("Некорректная координата")
    text = str(value)
    if len(text) > 32:
        raise ValueError("Слишком длинная координата")
    number = Decimal(text)
    low, high = (
        (Decimal("44.2"), Decimal("45.2"))
        if latitude
        else (
            Decimal("19.8"),
            Decimal("21.1"),
        )
    )
    if not number.is_finite() or not low <= number <= high:
        raise ValueError("Координата вне области Белграда")
    return format(number, "f")


def _location(point: dict[str, Any]) -> tuple[str, str]:
    return _coordinate(point.get("lat"), latitude=True), _coordinate(
        point.get("lon"), latitude=False
    )


def _photon_address(address: dict[str, str], payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return None
    features = payload.get("features")
    if not isinstance(features, list) or len(features) > _PHOTON_LIMIT:
        return None
    found: dict[tuple[str, int], tuple[str, str]] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props, geometry = feature.get("properties"), feature.get("geometry")
        if not isinstance(props, dict) or not isinstance(geometry, dict):
            continue
        district, county = props.get("district"), props.get("county")
        street, number = props.get("street"), props.get("housenumber")
        if (not isinstance(district, str) or not isinstance(county, str)
                or not isinstance(street, str) or not isinstance(number, str)):
            continue
        if (props.get("countrycode") != "RS" or props.get("type") != "house"
                or _name(county) != "град београд"
                or _municipality({"municipality": district}) != _municipality(address)
                or _name(street) != _name(address["street"])
                or _name(number) != _name(address["number"])):
            continue
        kind, identifier = props.get("osm_type"), props.get("osm_id")
        building = props.get("osm_key") == "building" and props.get("osm_value") != "no"
        address_node = (kind == "N" and props.get("osm_key") == "place"
                        and props.get("osm_value") == "house")
        if (kind not in ("N", "W") or type(identifier) is not int or identifier <= 0
                or not (building or address_node)):
            continue
        coords = geometry.get("coordinates")
        if (geometry.get("type") != "Point" or not isinstance(coords, list)
                or len(coords) != 2):
            continue
        try:
            location = _location({"lat": coords[1], "lon": coords[0]})
        except (ValueError, InvalidOperation):
            continue
        key = (kind, identifier)
        if key in found and found[key] != location:
            return None
        found[key] = location
    if len(found) != 1:
        return None
    (kind, identifier), (lat, lon) = next(iter(found.items()))
    osm_type = "node" if kind == "N" else "way"
    return {"address": dict(address), "latitude": lat, "longitude": lon,
            "osm_url": f"https://www.openstreetmap.org/{osm_type}/{identifier}"}


async def resolve_power_points(
    client: httpx.AsyncClient, addresses: list[dict[str, str]],
) -> list[dict[str, Any]] | None:
    """Точные дома по Photon. None означает, что сервис не ответил."""
    points: list[dict[str, Any]] = []
    checked = 0
    for entry in addresses[:MAX_INPUTS]:
        address = _valid_input(entry, segment=False)
        if address is None:
            continue
        if checked >= _POWER_QUERY_LIMIT:
            break
        try:
            async with asyncio.timeout(3):
                async with client.stream(
                    "GET", PHOTON_URL, params={
                        "q": (f'{address["street"]} {address["number"]}, '
                              f'{address["municipality"]}, Beograd'),
                        "limit": _PHOTON_LIMIT, "bbox": "19.8,44.2,21.1,45.2",
                        "countrycode": "RS",
                    }, follow_redirects=False, headers={"User-Agent": _USER_AGENT,
                                                        "Accept-Encoding": "identity"},
                ) as response:
                    response.raise_for_status()
                    payload = await _json_response(response)
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            return points if checked else None
        checked += 1
        if point := _photon_address(address, payload):
            points.append(point)
    return points


def _tags(element: dict[str, Any]) -> dict[str, str]:
    tags = element.get("tags")
    if not isinstance(tags, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in tags.items()
    ):
        return {}
    return tags


def _elements(payload: Any, municipality: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "remark" in payload:
        return []
    elements = payload.get("elements")
    if not isinstance(elements, list) or len(elements) > MAX_ELEMENTS:
        return []
    if any(
        not isinstance(item, dict)
        or type(item.get("id")) is not int
        or item["id"] <= 0
        or not isinstance(item.get("type"), str)
        or item["type"] not in {"node", "way", "relation"}
        for item in elements
    ):
        return []
    boundaries = [item for item in elements if item["type"] == "relation"]
    if len(boundaries) != 1:
        return []
    tags = _tags(boundaries[0])
    if (
        tags.get("boundary") != "administrative"
        or tags.get("admin_level") != "8"
        or _name(tags.get("name", "")) not in {municipality, "градска општина " + municipality}
    ):
        return []
    # Один объект может выйти из двух блоков запроса; противоречивые версии запрещены.
    unique: dict[tuple[str, int], dict[str, Any]] = {}
    for item in elements:
        key = (item["type"], item["id"])
        previous = unique.get(key, {})
        if any(previous[field] != item[field] for field in previous.keys() & item.keys()):
            return []
        unique[key] = {**previous, **item}
    return list(unique.values())


def _address_point(
    address: dict[str, str], elements: list[dict[str, Any]]
) -> dict[str, Any] | None:
    matches = []
    local_nodes = {item["id"] for item in elements if item["type"] == "node"}
    for element in elements:
        tags = _tags(element)
        if (
            element["type"] not in {"node", "way"}
            or "addr:interpolation" in tags
            or _name(tags.get("addr:street", "")) != _name(address["street"])
            or _name(tags.get("addr:housenumber", "")) != _name(address["number"])
        ):
            continue
        matches.append(element)
    if len(matches) != 1:
        return None
    element = matches[0]
    tags = _tags(element)
    if element["type"] == "way":
        nodes = element.get("nodes")
        if (
            not tags.get("building")
            or tags["building"] == "no"
            or not isinstance(nodes, list)
            or len(nodes) < 4
            or nodes[0] != nodes[-1]
            or any(type(node) is not int or node not in local_nodes for node in nodes)
        ):
            return None
        point = element.get("center")
    else:
        point = element
    if not isinstance(point, dict):
        return None
    try:
        latitude, longitude = _location(point)
    except (ValueError, InvalidOperation):
        return None
    return {
        "address": dict(address),
        "latitude": latitude,
        "longitude": longitude,
        "osm_url": f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
    }


def _path(
    graph: dict[int, set[int]], start: int, end: int, blocked: frozenset[int] = frozenset()
) -> list[int] | None:
    previous: dict[int, int | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == end:
            path = [end]
            while previous[path[-1]] is not None:
                parent = previous[path[-1]]
                assert parent is not None
                path.append(parent)
            return path[::-1]
        for neighbour in sorted(graph.get(node, set())):
            if neighbour not in previous and frozenset((node, neighbour)) != blocked:
                previous[neighbour] = node
                queue.append(neighbour)
    return None


def _segment_line(segment: dict[str, str], elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    available = {
        _name(_tags(element).get("name", ""))
        for element in elements
        if element["type"] == "way" and _tags(element).get("highway")
    }
    names = {}
    for key in ("street", "start", "end"):
        choices = (
            {_name(segment[key])} if key == "street" else _cross_street_names(segment[key])
        ) & available
        if len(choices) != 1:
            return None
        names[key] = next(iter(choices))
    if len(set(names.values())) != 3:
        return None
    coordinates: dict[int, tuple[str, str]] = {}
    memberships: dict[str, set[int]] = {name: set() for name in names.values()}
    graph: dict[int, set[int]] = {}
    edges: dict[frozenset[int], set[int]] = {}
    try:
        for element in elements:
            tags = _tags(element)
            name = _name(tags.get("name", ""))
            if element["type"] != "way" or not tags.get("highway") or name not in memberships:
                continue
            if tags["highway"] in {"proposed", "construction"} or tags.get("area") == "yes":
                return None
            nodes, geometry = element.get("nodes"), element.get("geometry")
            if (
                not isinstance(nodes, list)
                or not 2 <= len(nodes) <= MAX_ROAD_NODES
                or any(type(node) is not int or node <= 0 for node in nodes)
                or not isinstance(geometry, list)
                or len(geometry) != len(nodes)
            ):
                return None
            for node, point in zip(nodes, geometry, strict=True):
                if not isinstance(point, dict):
                    return None
                location = _location(point)
                if node in coordinates and coordinates[node] != location:
                    return None
                coordinates[node] = location
            if len(coordinates) > MAX_ROAD_NODES:
                return None
            memberships[name].update(nodes)
            if name == names["street"]:
                for left, right in zip(nodes, nodes[1:], strict=False):
                    if left == right:
                        return None
                    graph.setdefault(left, set()).add(right)
                    graph.setdefault(right, set()).add(left)
                    edges.setdefault(frozenset((left, right)), set()).add(element["id"])
    except (ValueError, InvalidOperation):
        return None
    start = memberships[names["street"]] & memberships[names["start"]]
    end = memberships[names["street"]] & memberships[names["end"]]
    if len(start) != 1 or len(end) != 1 or start == end:
        return None
    start_node, end_node = next(iter(start)), next(iter(end))
    path = _path(graph, start_node, end_node)
    if path is None or not 2 <= len(path) <= 1000:
        return None
    local_nodes = {item["id"] for item in elements if item["type"] == "node"}
    if any(node not in local_nodes for node in path):
        return None
    used = set()
    for left, right in zip(path, path[1:], strict=False):
        edge = frozenset((left, right))
        # Каждый участок должен быть мостом между двумя сторонами: иначе есть
        # другой топологический путь и нельзя выбрать его произвольно.
        if len(edges[edge]) != 1 or _path(graph, start_node, end_node, edge) is not None:
            return None
        used.update(edges[edge])
    return {
        "segment": dict(segment),
        "coordinates": [list(coordinates[node]) for node in path],
        "osm_urls": [f"https://www.openstreetmap.org/way/{way}" for way in sorted(used)],
    }


async def _json_response(response: httpx.Response) -> Any:
    if response.status_code != 200:
        raise ValueError("Неполный ответ географического источника")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("Сжатый ответ географического источника не принимается")
    length = response.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
        raise ValueError("Ответ географического источника слишком велик")
    body = bytearray()
    async for part in response.aiter_bytes(chunk_size=64 * 1024):
        body.extend(part)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Ответ географического источника слишком велик")
    return json.loads(body, parse_float=Decimal)


async def _fetch(client: httpx.AsyncClient, query: str) -> Any:
    async with asyncio.timeout(REQUEST_TIMEOUT):
        async with client.stream(
            "POST",
            OVERPASS_URL,
            data={"data": query},
            follow_redirects=False,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=4),
        ) as response:
            response.raise_for_status()
            return await _json_response(response)


async def resolve_utility_geometry(
    client: httpx.AsyncClient,
    addresses: list[dict[str, str]],
    segments: list[dict[str, str]],
) -> dict[str, Any]:
    """До 8 заявленных адресов/участков, один запрос на муниципалитет, без повторов.

    Геометрия OSM — отдельное картографическое свидетельство, а не подтверждение
    отключения. Для здания-way точка является центром его OSM bounding box.
    Объявления с уточнением населённого пункта, диапазоны и неточные имена пропускаются.
    """
    result: dict[str, Any] = {"points": [], "lines": []}
    if (
        not isinstance(addresses, list)
        or not isinstance(segments, list)
        or len(addresses) + len(segments) > MAX_INPUTS
    ):
        return result
    valid_addresses = [
        entry for value in addresses if (entry := _valid_input(value, segment=False)) is not None
    ]
    valid_segments = [
        entry for value in segments if (entry := _valid_input(value, segment=True)) is not None
    ]
    municipalities = sorted({_municipality(value) for value in valid_addresses + valid_segments})
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            for municipality in municipalities:
                local_addresses = [a for a in valid_addresses if _municipality(a) == municipality]
                local_segments = [s for s in valid_segments if _municipality(s) == municipality]
                try:
                    payload = await _fetch(
                        client, _query(municipality, local_addresses, local_segments)
                    )
                    elements = _elements(payload, municipality)
                except (
                    httpx.HTTPError,
                    TimeoutError,
                    ValueError,
                    InvalidOperation,
                    RecursionError,
                ):
                    continue
                for address in local_addresses:
                    if (point := _address_point(address, elements)) is not None:
                        result["points"].append(point)
                for segment in local_segments:
                    if (line := _segment_line(segment, elements)) is not None:
                        result["lines"].append(line)
    except TimeoutError:
        pass
    return result
