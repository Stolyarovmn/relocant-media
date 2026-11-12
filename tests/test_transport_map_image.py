"""Проверка геометрии и воспроизводимых PNG на синтетических координатах."""

import binascii
import struct
import zlib
from decimal import localcontext

import pytest

from relocant_media.services.transport_map_image import (
    MAP_HEIGHT,
    MAP_WIDTH,
    _layout,
    _project,
    render_transport_map,
)

LINE = [
    ["44.6500", "20.3500"],
    ["44.6520", "20.3520"],
    ["44.6510", "20.3570"],
    ["44.6540", "20.3600"],
]


def decode(png: bytes) -> tuple[tuple[int, ...], bytes]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    chunks = []
    while offset < len(png):
        size = struct.unpack(">I", png[offset : offset + 4])[0]
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + size]
        crc = struct.unpack(">I", png[offset + 8 + size : offset + 12 + size])[0]
        assert crc == binascii.crc32(kind + payload) & 0xFFFFFFFF
        chunks.append((kind, payload))
        offset += size + 12
    assert offset == len(png)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    header = struct.unpack(">IIBBBBB", chunks[0][1])
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == MAP_HEIGHT * (1 + MAP_WIDTH * 3)
    assert all(raw[row * (1 + MAP_WIDTH * 3)] == 0 for row in range(MAP_HEIGHT))
    return header, raw


def pixel(raw: bytes, x: int, y: int) -> tuple[int, ...]:
    offset = y * (1 + MAP_WIDTH * 3) + 1 + x * 3
    return tuple(raw[offset : offset + 3])


def test_png_is_valid_rgb_and_reproducible_without_input_mutation():
    before = [point[:] for point in LINE]
    first = render_transport_map(LINE)
    assert first == render_transport_map(LINE)
    header, raw = decode(first)
    assert header == (1200, 630, 8, 2, 0, 0, 0)
    assert LINE == before
    assert pixel(raw, 0, 0) == (245, 247, 249)
    assert len(first) < 100_000


def test_decimal_equivalents_and_context_do_not_change_pixels():
    alternate = [
        ["44.65", "2.035E1"],
        ["44.652", "20.35200"],
        ["44.651", "20.357"],
        ["44.654", "20.36000"],
    ]
    expected = render_transport_map(LINE)
    with localcontext() as context:
        context.prec = 3
        assert render_transport_map(alternate) == expected


def test_north_is_up_east_is_right_and_metric_aspect_ratio_is_preserved():
    # На 60° широты 0,01° долготы ≈556м, а 0,01° широты ≈1112м.
    line = [["60", "20"], ["60", "20.01"], ["60.01", "20.01"]]
    projected = _project(line)
    east = projected[1][0] - projected[0][0]
    north = projected[2][1] - projected[1][1]
    assert east == pytest.approx(556, rel=0.002)
    assert north == pytest.approx(1112, rel=0.002)
    points = _layout(line).points
    assert points[1][0] > points[0][0]
    assert points[2][1] < points[1][1]
    assert points[1][1] == points[0][1]
    assert (points[1][0] - points[0][0]) / (points[1][1] - points[2][1]) == pytest.approx(
        0.5,
        abs=0.01,
    )


def test_antimeridian_is_short_local_segment_not_world_wide_line():
    projected = _project([["10", "179.999"], ["10", "-179.999"]])
    assert projected[1][0] - projected[0][0] == pytest.approx(219, abs=1)
    decode(render_transport_map([["10", "179.999"], ["10", "-179.999"]]))


def test_scale_bar_matches_projected_metric_scale():
    layout = _layout(LINE)
    _, raw = decode(render_transport_map(LINE))
    expected_length = round(layout.scale_meters * layout.pixels_per_meter)
    assert 90 <= expected_length <= 240
    assert pixel(raw, 96 + expected_length // 2, 560) == (27, 44, 63)
    assert pixel(raw, 96 + expected_length, 569) == (27, 44, 63)
    assert pixel(raw, 96 + expected_length + 8, 560) == (245, 247, 249)


def test_endpoint_markers_follow_input_order_not_latitude_sorting():
    png = render_transport_map(LINE)
    _, raw = decode(png)
    first, last = _layout(LINE).points[0], _layout(LINE).points[-1]
    assert pixel(raw, first[0] + 12, first[1]) == (18, 92, 180)
    assert pixel(raw, last[0] + 12, last[1]) == (183, 65, 25)
    assert render_transport_map(LINE[::-1]) != png


@pytest.mark.parametrize(
    "line",
    [
        [],
        [["44", "20"]],
        [["44", "20"]] * 1001,
        None,
        [["44", "20", "10"], ["45", "20"]],
        [[44, "20"], ["44.01", "20"]],
        [["NaN", "20"], ["44", "20"]],
        [["sNaN", "20"], ["44", "20"]],
        [["Infinity", "20"], ["44", "20"]],
        [["44", "-181"], ["44", "20"]],
        [["85.001", "20"], ["85", "20"]],
        [["-85.001", "20"], ["-85", "20"]],
        [["44 ", "20"], ["44.01", "20"]],
        [["4_4", "20"], ["44.01", "20"]],
        [["0" * 65, "20"], ["44", "20"]],
        [["44", "20"], ["44", "20"]],
        [["44", "20"], ["44.00000001", "20"]],
        [["44", "20"], ["45", "22"]],
        [["84.5", "20"], ["85", "20"]],
    ],
)
def test_invalid_degenerate_or_nonlocal_input_fails_closed(line):
    with pytest.raises(ValueError):
        render_transport_map(line)


@pytest.mark.parametrize(
    "line",
    [
        [["44", "20"], ["44", "20.01"]],
        [["44", "20"], ["44.01", "20"]],
        [["44", "20"], ["44", "20"], ["44.01", "20"]],
        [["44", "20"], ["44.01", "20.01"], ["44.01", "20"], ["44", "20"]],
        [["-33.9", "18.4"], ["-33.91", "18.42"]],
    ],
)
def test_horizontal_vertical_closed_and_southern_lines_are_valid(line):
    decode(render_transport_map(line))


def test_maximum_point_count_is_supported():
    # Повторный ход не размножает работу толстого штриха на пересечениях.
    line = [["44.65", "20.35"], ["44.654", "20.36"]] * 500
    decode(render_transport_map(line))
