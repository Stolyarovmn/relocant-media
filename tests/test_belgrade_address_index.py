import gzip
import json
import re
from datetime import datetime
from pathlib import Path

from relocant_media.ops.belgrade_address_index import _MUNICIPALITY_NAMES, _address, _encoded_gzip


def test_address_index_requires_a_tagged_house_number() -> None:
    assert _address({"addr:street": " Војводе   Шупљикца ", "addr:housenumber": "42А"}) == (
        "Војводе Шупљикца", "42А",
    )
    assert _address({"addr:place": "Болеч", "addr:housenumber": "42a"}) == ("Болеч", "42a")
    assert _address({"addr:street": "Војводе Шупљикца", "addr:housenumber": "12-18"}) is None
    assert _address({
        "addr:street": "Војводе Шупљикца",
        "addr:housenumber": "12",
        "addr:interpolation": "odd",
    }) is None
    assert _address({"addr:housenumber": "12"}) is None


def test_gzip_is_deterministic_and_decodes_as_normal_json() -> None:
    payload = b'{"version":1,"addresses":[]}'
    assert gzip.decompress(_encoded_gzip(payload)) == payload
    assert _encoded_gzip(payload) == _encoded_gzip(payload)


def test_bundled_city_index_is_attributed_and_covers_all_municipalities() -> None:
    bundle = Path("cities/belgrade/map/addresses.json.gz")
    with gzip.open(bundle, "rt", encoding="utf-8") as input_file:
        index = json.load(input_file)
    assert index["version"] == 1
    assert index["source"]["license"] == "ODbL-1.0"
    assert re.fullmatch(r"[0-9a-f]{64}", index["source"]["extract_sha256"])
    assert datetime.fromisoformat(index["source"]["snapshot_at"]).tzinfo is not None

    rows = index["addresses"]
    assert len(rows) >= 10_000
    assert {row[2] for row in rows} == set(_MUNICIPALITY_NAMES)
    assert all(44.2 < lat < 45.2 and 19.8 < lon < 21.1 for _, _, _, lat, lon in rows)


def test_bundled_road_index_preserves_shared_osm_nodes() -> None:
    with gzip.open(Path("cities/belgrade/map/roads.json.gz"), "rt", encoding="utf-8") as input_file:
        index = json.load(input_file)
    assert index["version"] == 1
    assert index["source"]["license"] == "ODbL-1.0"
    assert set(index["municipalities"]) == set(_MUNICIPALITY_NAMES)
    for ways in index["municipalities"].values():
        nodes: dict[int, tuple[float, float]] = {}
        assert ways
        for way_id, name, geometry in ways:
            assert way_id > 0 and name and len(geometry) >= 2
            for node_id, lat, lon in geometry:
                assert node_id > 0 and 44.2 < lat < 45.2 and 19.8 < lon < 21.1
                assert nodes.setdefault(node_id, (lat, lon)) == (lat, lon)
