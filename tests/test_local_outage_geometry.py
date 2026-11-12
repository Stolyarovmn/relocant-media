import gzip
import json
from datetime import date
from pathlib import Path

from relocant_media.ops.belgrade_rgz_index import parse_report
from relocant_media.services.local_outage_geometry import load_area_index, within_outage_segment


def test_exact_house_and_settlement_keep_their_verified_geometry(tmp_path: Path) -> None:
    footprint = [[[44.76, 20.41], [44.76, 20.411], [44.761, 20.411], [44.76, 20.41]]]
    border = [[[44.75, 20.40], [44.75, 20.43], [44.78, 20.43],
               [44.78, 20.40], [44.75, 20.40]]]
    index_path = tmp_path / "areas.gz"
    index_path.write_bytes(gzip.compress(json.dumps({
        "version": 1,
        "addresses": [
            ["Водоводска", "21", "Čukarica", 44.7601, 20.4104, "n1", "w7"],
            ["Водоводска", "23", "Čukarica", 44.7901, 20.4104, "n2", None],
            ["Водоводска", "25", "Zemun", 44.86, 20.41, "n3", None],
        ],
        "buildings": {"w7": footprint},
        "settlements": [["Čukarica", ["Жарково"], "r17", [border]]],
    }).encode()))
    index = load_area_index(index_path)
    house = index.address("Vodovodska", "21", "Čukarica, naselje Žarkovo")
    assert house is not None
    assert house.point == (44.7601, 20.4104)
    assert house.osm_url == "https://www.openstreetmap.org/node/1"
    assert house.buildings[0].osm_url == "https://www.openstreetmap.org/way/7"
    assert [item.number for item in index.street("Водоводска", "Чукарица, насеље Жарково")] == [
        "21",
    ]
    assert index.address("Vodovodska", "23", "Čukarica, naselje Žarkovo") is None
    assert index.address("Vodovodska", "25", "Čukarica") is None
    assert not index.street("Vodovodska", "Čukarica, naselje Другой")
    assert index.settlement("Žarkovo", "Čukarica")[0].osm_url.endswith("/relation/17")
    assert not index.settlement("Žarkovo", "Zemun")


def test_house_must_be_along_the_published_part_of_the_same_street() -> None:
    line = ((44.76, 20.410), (44.76, 20.412))
    assert within_outage_segment((44.7601, 20.411), line)
    assert not within_outage_segment((44.7601, 20.409), line)
    assert not within_outage_segment((44.7601, 20.412), line)
    assert not within_outage_segment((44.7608, 20.411), line)


def test_invalid_archive_does_not_supply_geometry(tmp_path: Path) -> None:
    path = tmp_path / "invalid.gz"
    path.write_bytes(gzip.compress(b'{"version":1,"buildings":{"w1":[[44,20]]}}'))
    index = load_area_index(path)
    assert not index.street("Водоводска", "Чукарица")
    assert not index.settlement("Жарково", "Чукарица")


def test_public_cadastre_snapshot_supplies_only_a_point(tmp_path: Path) -> None:
    report_path = tmp_path / "report.html"
    report_path.write_text(
        "<h2>Izveštaj za BEOGRAD (NOVI BEOGRAD)</h2><table>"
        "<tr><td>3201</td><td>Токарска 12</td><td>44.8062, 20.363</td>"
        "<td></td><td></td><td></td><td></td></tr></table>"
        "<footer>RGZ podaci od 05.09.2026.</footer>",
        encoding="utf-8",
    )
    url = "https://dina.openstreetmap.rs/ar/opstine/novibeograd/report.html"
    report = parse_report(report_path, "Novi Beograd", "BEOGRAD (NOVI BEOGRAD)", url)
    rgz_path = tmp_path / "rgz-addresses.json.gz"
    rgz_path.write_bytes(gzip.compress(json.dumps({"version": 1, "reports": [report]}).encode()))
    index = load_area_index(tmp_path / "areas.gz")
    address = index.address("Tokarska", "12", "Novi Beograd")
    assert address is not None
    assert address.point == (44.8062, 20.363)
    assert address.source_url == url
    assert address.source_id == "3201"
    assert address.snapshot_at == date(2026, 9, 5)
    assert address.osm_url is None
    assert not address.buildings
