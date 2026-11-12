"""Синтетические ответы Overpass; сеть и настоящие адреса в тестах не нужны."""

import asyncio
import copy
import json
from urllib.parse import parse_qs

import httpx
import pytest

from relocant_media.services import utility_geography as geography


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, expected", [
    ({"osm_key": "amenity", "osm_value": "restaurant"}, True),
    ({"housenumber": "42a"}, True),
    ({"district": "Звездара"}, True),
    ({}, False),
])
async def test_photon_accepts_only_a_unique_exact_house(extra, expected):
    address = {"municipality": "Vračar", "street": "Vojvode Šupljikca", "number": "42"}
    properties = {"type": "house", "countrycode": "RS", "county": "Град Београд",
                  "district": "Врачар", "street": "Војводе Шупљикца", "housenumber": "42",
                  "osm_type": "W", "osm_id": 398033226, "osm_key": "building",
                  "osm_value": "yes"}
    location = {"type": "Point", "coordinates": [20.4860688, 44.7983131]}
    features = [
        {"type": "Feature", "properties": properties, "geometry": location},
        {"type": "Feature", "properties": {**properties, "osm_id": 42, **extra},
         "geometry": location},
    ]

    def handler(request):
        assert request.method == "GET" and request.url.host == "photon.komoot.io"
        assert request.url.params["q"] == "Vojvode Šupljikca 42, Vračar, Beograd"
        return httpx.Response(200, json={"type": "FeatureCollection", "features": features})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        points = await geography.resolve_power_points(client, [address])
    assert points == ([{"address": address, "latitude": "44.7983131",
                        "longitude": "20.4860688", "osm_url":
                        "https://www.openstreetmap.org/way/398033226"}] if expected else [])

ADDRESS = {"municipality": "Vračar", "street": "Primer ulice", "number": "42"}
SEGMENT = {
    "municipality": "Voždovac",
    "street": "Glavna ulica",
    "start": "Prva ulica",
    "end": "Druga ulica",
}


def boundary(name="врачар"):
    return {
        "type": "relation",
        "id": 9000,
        "tags": {
            "name": "Градска општина " + name,
            "boundary": "administrative",
            "admin_level": "8",
        },
    }


def address_node(identifier=42, **changes):
    result = {
        "type": "node",
        "id": identifier,
        "lat": 44.8,
        "lon": 20.48,
        "tags": {"addr:street": "Пример улице", "addr:housenumber": "42"},
    }
    result.update(changes)
    return result


def location(node):
    return {"lat": round(44.75 + node / 1000, 6), "lon": round(20.47 + node / 10000, 6)}


def road(identifier, name, nodes):
    return {
        "type": "way",
        "id": identifier,
        "nodes": nodes,
        "geometry": [location(node) for node in nodes],
        "tags": {"name": name, "highway": "residential"},
    }


def roads():
    ways = [
        road(100, "Главна улица", [1, 2, 3, 4]),
        road(200, "Прва улица", [5, 2, 6]),
        road(300, "Друга улица", [7, 4, 8]),
    ]
    return [
        boundary("вождовац"),
        *ways,
        *[{"type": "node", "id": node, **location(node)} for node in range(1, 10)],
    ]


async def resolve(elements, addresses=None, segments=None):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"elements": elements})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await geography.resolve_utility_geometry(
            client,
            [ADDRESS] if addresses is None else addresses,
            segments or [],
        )
    return result, requests


@pytest.mark.asyncio
async def test_exact_address_preserves_source_and_one_bounded_request():
    source = copy.deepcopy(ADDRESS)
    result, requests = await resolve([boundary(), address_node()])
    assert result == {
        "points": [
            {
                "address": ADDRESS,
                "latitude": "44.8",
                "longitude": "20.48",
                "osm_url": "https://www.openstreetmap.org/node/42",
            }
        ],
        "lines": [],
    }
    assert ADDRESS == source
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == geography.OVERPASS_URL
    assert request.method == "POST"
    assert "RelocantMedia" in request.headers["User-Agent"]
    query = parse_qs(request.content.decode())["data"][0]
    assert "[timeout:8][maxsize:33554432]" in query
    assert '["admin_level"="8"]' in query
    assert all(word in query for word in ("градска", "општина", "врачар"))
    assert "(area.a)" in query
    assert "out body center" in query


@pytest.mark.asyncio
async def test_building_way_has_explicit_object_provenance_and_requires_local_vertices():
    way = {
        "type": "way",
        "id": 70,
        "nodes": [1, 2, 3, 1],
        "center": {"lat": 44.8, "lon": 20.48},
        "tags": {**address_node()["tags"], "building": "yes"},
    }
    vertices = [{"type": "node", "id": node, **location(node)} for node in (1, 2, 3)]
    result, _ = await resolve([boundary(), way, *vertices])
    assert result["points"][0]["osm_url"] == "https://www.openstreetmap.org/way/70"
    rejected, _ = await resolve([boundary(), way, *vertices[:-1]])
    assert rejected == {"points": [], "lines": []}


@pytest.mark.asyncio
async def test_serbian_latin_cyrillic_equivalence_does_not_strip_diacritics():
    address = {**ADDRESS, "street": "Đorđa Šupljikca", "number": "1А"}
    node = address_node(tags={"addr:street": "Ђорђа Шупљикца", "addr:housenumber": "1a"})
    result, _ = await resolve([boundary(), node], [address])
    assert len(result["points"]) == 1
    result, _ = await resolve([boundary(), node], [{**address, "street": "Dorda Supljikca"}])
    assert not result["points"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"number": "11-29"},
        {"number": "1A-1K"},
        {"number": "42/1"},
        {"municipality": "Vračar, naselje Primer"},
        {"municipality": "Niš"},
        {"street": "x\n;out;"},
        {"street": " "},
        {"number": 42},
    ],
)
async def test_ranges_unsupported_settlements_and_invalid_input_never_query(change):
    result, requests = await resolve([], [{**ADDRESS, **change}])
    assert result == {"points": [], "lines": []}
    assert requests == []


@pytest.mark.asyncio
async def test_too_many_inputs_fail_without_truncating_or_querying():
    result, requests = await resolve([], [ADDRESS] * 9)
    assert result == {"points": [], "lines": []}
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "elements",
    [
        [boundary("земун"), address_node()],
        [address_node()],
        [boundary(), {**boundary(), "id": 9001}, address_node()],
        [boundary(), address_node(), address_node(43)],
        [boundary(), address_node(), address_node(43, lat="invalid")],
        [boundary(), address_node(tags={"addr:street": "Пример улице", "addr:housenumber": "142"})],
        [boundary(), address_node(lat=True)],
        [boundary(), address_node(lat="NaN")],
        [boundary(), address_node(lat=40)],
        [boundary(), address_node(), address_node(lon=20.49)],
        [boundary(), address_node(type=[])],
    ],
)
async def test_no_guessing_when_address_boundary_or_coordinate_is_ambiguous(elements):
    result, _ = await resolve(elements)
    assert result == {"points": [], "lines": []}


@pytest.mark.asyncio
async def test_one_query_groups_multiple_addresses_and_roads_per_municipality():
    result, requests = await resolve(
        [boundary(), address_node()],
        [ADDRESS, {**ADDRESS, "number": "44"}],
        [{**SEGMENT, "municipality": "Врачар"}],
    )
    assert len(result["points"]) == 1
    assert len(requests) == 1
    query = parse_qs(requests[0].content.decode())["data"][0]
    assert "out body center" in query and "out body geom" in query
    assert "node(w.roads)(area.a)" in query


@pytest.mark.asyncio
async def test_unique_road_path_uses_actual_shared_nodes_not_straight_line():
    result, _ = await resolve(roads(), [], [SEGMENT])
    assert result["points"] == []
    assert result["lines"] == [
        {
            "segment": SEGMENT,
            "coordinates": [[str(location(n)["lat"]), str(location(n)["lon"])] for n in (2, 3, 4)],
            "osm_urls": ["https://www.openstreetmap.org/way/100"],
        }
    ]


@pytest.mark.asyncio
async def test_split_named_road_keeps_only_path_way_ids_and_direction():
    elements = roads()
    elements[1] = road(100, "Главна улица", [1, 2, 3])
    elements.extend([road(101, "Главна улица", [3, 4]), road(102, "Главна улица", [3, 9])])
    segment = {**SEGMENT, "start": SEGMENT["end"], "end": SEGMENT["start"]}
    result, _ = await resolve(elements, [], [segment])
    assert result["lines"][0]["osm_urls"] == [
        "https://www.openstreetmap.org/way/100",
        "https://www.openstreetmap.org/way/101",
    ]
    assert result["lines"][0]["coordinates"][0] == [
        str(location(4)["lat"]),
        str(location(4)["lon"]),
    ]


@pytest.mark.asyncio
async def test_two_narrow_genitive_forms_require_one_existing_exact_road_name():
    elements = roads()
    elements[2]["tags"]["name"] = "Видска"
    elements[3]["tags"]["name"] = "Крушевачка"
    segment = {**SEGMENT, "start": "Vidske", "end": "Kruševačke"}
    result, requests = await resolve(elements, [], [segment])
    assert len(result["lines"]) == 1
    assert result["lines"][0]["segment"] == segment
    query = parse_qs(requests[0].content.decode())["data"][0]
    assert "видска" in query and "видске" in query
    assert "крушевачка" in query and "крушевачке" in query
    elements.append(road(201, "Видске", [5, 2]))
    rejected, _ = await resolve(elements, [], [segment])
    assert not rejected["lines"]


@pytest.mark.asyncio
async def test_other_grammatical_or_spelling_changes_are_not_guessed():
    result, _ = await resolve(roads(), [], [{**SEGMENT, "start": "Prve ulice"}])
    assert not result["lines"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "cycle",
        "multiple_intersections",
        "disconnected",
        "coincident_only",
        "missing_geometry",
        "wrong_street",
        "conflicting_node",
        "outside_municipality",
        "duplicated_edge",
    ],
)
async def test_ambiguous_or_incomplete_road_path_is_rejected(failure):
    elements = roads()
    if failure == "cycle":
        elements.append(road(101, "Главна улица", [2, 9, 4]))
    elif failure == "multiple_intersections":
        elements[2] = road(200, "Прва улица", [2, 3])
    elif failure == "disconnected":
        elements[1] = road(100, "Главна улица", [1, 2])
        elements.append(road(101, "Главна улица", [3, 4]))
    elif failure == "coincident_only":
        elements[2] = road(200, "Прва улица", [5, 9, 6])
        elements[2]["geometry"][1] = location(2)
    elif failure == "missing_geometry":
        del elements[1]["geometry"]
    elif failure == "wrong_street":
        elements[1]["tags"]["name"] = "Друга главна улица"
    elif failure == "conflicting_node":
        elements[2]["geometry"][1]["lat"] = 44.9
    elif failure == "outside_municipality":
        elements = [e for e in elements if not (e["type"] == "node" and e["id"] == 3)]
    elif failure == "duplicated_edge":
        elements.append(road(101, "Главна улица", [2, 3]))
    result, _ = await resolve(elements, [], [SEGMENT])
    assert result == {"points": [], "lines": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["timeout", "redirect", "http", "malformed", "oversized", "remark"]
)
async def test_upstream_failures_are_bounded_empty_and_never_retried(failure):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://elsewhere.invalid"})
        if failure == "http":
            return httpx.Response(429)
        if failure == "malformed":
            return httpx.Response(200, content=b"invalid")
        if failure == "oversized":
            return httpx.Response(200, content=b" " * (geography.MAX_RESPONSE_BYTES + 1))
        return httpx.Response(
            200, json={"remark": "runtime error: timeout", "elements": [boundary(), address_node()]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await geography.resolve_utility_geometry(client, [ADDRESS], [])
    assert result == {"points": [], "lines": []}
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_whole_operation_has_a_deadline(monkeypatch):
    monkeypatch.setattr(geography, "TOTAL_TIMEOUT", 0.01)

    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, content=json.dumps({"elements": []}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await geography.resolve_utility_geometry(client, [ADDRESS], [])
    assert result == {"points": [], "lines": []}
