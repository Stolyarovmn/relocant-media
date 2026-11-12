"""Детерминированные примеры официального формата, без сети и выполнения JavaScript."""

import json
from typing import Any

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services import transport_map_source as source


def document(**changes: Any) -> EvidenceDocument:
    return EvidenceDocument.model_validate({
        "source_id": "bgprevoz_changes",
        "url": "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151",
        "title": "Измена рада линија код окретнице Сремчица",
        "body": "Радови на окретници Сремчица.",
        **changes,
    })


def data(**changes: Any) -> dict[str, Any]:
    return {
        "id": 1151, "mapa_izmene": 1,
        "lokacija_sirina": "44.650515", "lokacija_duzina": "20.404065",
        "zona_radova": None, **changes,
    }


def page(value: dict[str, Any] | None = None, *, container: str = '<div id="gmap-r"></div>') -> str:
    return container + "<script>\n const izmena = " + json.dumps(
        value if value is not None else data(),
    ) + ";\n const publicMapProvider = 'leaflet';</script>"


def test_explicit_point_without_invented_line() -> None:
    assert source.extract_transport_map(document(), page()) == {
        "latitude": "44.650515", "longitude": "20.404065", "line": [],
    }


def test_line_preserves_decimal_precision_order_and_geometry() -> None:
    zone = ('{"coordinates":[{"lat":44.82907754030629,"lng":20.383273009791218},'
            '{"lat":44.829245102519174,"lng":20.38271505773094}]}')
    result = source.extract_transport_map(document(), page(data(zona_radova=zone)))
    assert result == {
        "latitude": "44.650515", "longitude": "20.404065",
        "line": [["44.82907754030629", "20.383273009791218"],
                 ["44.829245102519174", "20.38271505773094"]],
    }


def test_unrelated_dynamic_fields_and_other_notices_do_not_affect_output() -> None:
    original = source.extract_transport_map(document(), page())
    html = '<script>let izmene = [{"id":999,"zona_radova":"wrong"}];</script>'
    html += page(data(updated_at="2027-01-01", izmena_linije=[{"id":99}], stajalista=[]))
    assert source.extract_transport_map(document(), html) == original


@pytest.mark.parametrize("url", [
    "http://www.bgprevoz.rs/linije/aktuelne-izmene/1151",
    "https://www.bgprevoz.rs.evil.test/linije/aktuelne-izmene/1151",
    "https://www.bgprevoz.rs@evil.test/linije/aktuelne-izmene/1151",
    "https://evil.test@www.bgprevoz.rs/linije/aktuelne-izmene/1151",
    "https://www.bgprevoz.rs:443/linije/aktuelne-izmene/1151",
    "https://127.0.0.1/linije/aktuelne-izmene/1151",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151/511",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151/",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151?x=1",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151#map",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151?",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151#",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/%31%31%35%31",
    "https://www.bgprevoz.rs/linije/aktuelne-izmene/01151",
    "https://www.bgprevoz.rs\n/linije/aktuelne-izmene/1151",
])
def test_only_exact_official_notice_url(url: str) -> None:
    assert source.extract_transport_map(document(url=url), page()) is None


def test_source_identity_and_both_allowed_hosts() -> None:
    assert source.extract_transport_map(document(source_id="other_source"), page()) is None
    assert source.extract_transport_map(
        document(url="https://bgprevoz.rs/linije/aktuelne-izmene/1151"), page(),
    ) is not None


@pytest.mark.parametrize("changes", [
    {"id": 1152}, {"id": "1151"}, {"id": 1151.0}, {"id": True},
    {"mapa_izmene": 0}, {"mapa_izmene": True}, {"mapa_izmene": "1"},
    {"lokacija_sirina": None}, {"lokacija_sirina": True}, {"lokacija_sirina": []},
    {"lokacija_sirina": "NaN"}, {"lokacija_sirina": "Infinity"},
    {"lokacija_sirina": 90.1}, {"lokacija_duzina": -180.1},
    {"lokacija_sirina": "44.5;alert(1)"}, {"lokacija_sirina": " 44.5"},
])
def test_invalid_point_or_notice_fails_closed(changes: dict[str, Any]) -> None:
    assert source.extract_transport_map(document(), page(data(**changes))) is None


@pytest.mark.parametrize("zone", [
    "", "null", "[]", "{}", {}, [],
    '{"coordinates":[]}', '{"coordinates":[{"lat":44,"lng":20}]}',
    '{"coordinates":[[44,20],[45,21]]}',
    '{"coordinates":[{"lat":44,"lng":20},{"lat":44,"lng":20}]}',
    '{"coordinates":[{"lat":44,"lng":20},{"lat":91,"lng":20}]}',
    '{"coordinates":[{"lat":44,"lng":20},{"lat":null,"lng":20}]}',
    '{"coordinates":[{"lat":44,"lng":20},{"lat":NaN,"lng":20}]}',
    '{"coordinates":[{"lat":44,"lng":20},{"lat":45,"lat":46,"lng":21}]}',
    '{"coordinates":[],"coordinates":[{"lat":44,"lng":20},{"lat":45,"lng":21}]}',
    '{"coordinates":[{"lat":44,"lng":20},{"lat":45,"lng":21}],"type":"Polygon"}',
])
def test_bad_provided_line_never_degrades_to_point(zone: object) -> None:
    assert source.extract_transport_map(document(), page(data(zona_radova=zone))) is None


@pytest.mark.parametrize("container", [
    "", '<div id="gmap"></div>', '<span id="gmap-r"></span>',
    '<div id="gmap-r"></div><div id="gmap-r"></div>',
    '<div id="gmap-r" hidden></div>', '<div id="gmap-r" aria-hidden="true"></div>',
    '<div class="hidden"><div id="gmap-r"></div></div>',
    '<div style="display: none !important"><div id="gmap-r"></div></div>',
    '<div id="gmap-r" style="visibility:hidden"></div>',
    '<div id="gmap-r" style="opacity:0"></div>',
    '<template><div id="gmap-r"></div></template>',
])
def test_visible_notice_map_container_required(container: str) -> None:
    assert source.extract_transport_map(document(), page(container=container)) is None


@pytest.mark.parametrize("replacement", [
    'const izmena = {"id":1151,"id":1151,',
    'const izmena = {"irrelevant":NaN,',
    'const izmena = {"irrelevant":Infinity,',
])
def test_duplicate_keys_and_nonfinite_json_rejected(replacement: str) -> None:
    html = page().replace('const izmena = {', replacement)
    assert source.extract_transport_map(document(), html) is None


@pytest.mark.parametrize("transform", [
    lambda html: html + html,
    lambda html: html.replace("<script>", '<script src="/external.js">'),
    lambda html: html.replace("<script>", '<script type="application/ld+json">'),
    lambda html: html.replace("const izmena =", "// const izmena ="),
    lambda html: html.replace("const izmena =", "let izmena ="),
    lambda html: html.replace("const izmena =", "const text = 'const izmena ="),
    lambda html: html.replace(";\n const public", " + getData();\n const public"),
    lambda html: html.replace(";\n const public", "\n const public"),
])
def test_no_javascript_evaluation_or_ambiguous_declaration(transform: Any) -> None:
    assert source.extract_transport_map(document(), transform(page())) is None


def test_multiple_notice_declarations_with_single_container_are_rejected() -> None:
    html = page() + page(container="")
    assert source.extract_transport_map(document(), html) is None


def test_missing_point_cannot_be_replaced_with_line_or_default_center() -> None:
    value = data(zona_radova='{"coordinates":[{"lat":44,"lng":20},{"lat":45,"lng":21}]}')
    del value["lokacija_sirina"]
    assert source.extract_transport_map(document(), page(value)) is None


def test_decimal_canonicalization_and_bounds() -> None:
    assert source.extract_transport_map(
        document(), page(data(lokacija_sirina=-90, lokacija_duzina="180.00000")),
    ) == {"latitude": "-90", "longitude": "180", "line": []}


def test_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "MAX_HTML_BYTES", 10)
    assert source.extract_transport_map(document(), page()) is None
    monkeypatch.setattr(source, "MAX_HTML_BYTES", 10_000)
    monkeypatch.setattr(source, "MAX_JSON_BYTES", 10)
    assert source.extract_transport_map(document(), page()) is None


def test_line_vertex_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source, "MAX_LINE_POINTS", 2)
    zone = json.dumps({"coordinates": [{"lat": 44 + i, "lng": 20} for i in range(3)]})
    assert source.extract_transport_map(document(), page(data(zona_radova=zone))) is None
