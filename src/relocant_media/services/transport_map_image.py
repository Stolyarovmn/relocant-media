"""Детерминированная схема опубликованной линии: без сети, подложки и геокодирования.

Локальная сферическая равнопромежуточная цилиндрическая проекция:
x = R Δλ cos φ₀, y = R Δφ; φ₀ — середина диапазона широт.
Формула: https://proj.org/en/stable/operations/projections/eqc.html
Масштаб приблизительный; слишком широкие и приполярные области отвергаются.
Маркеры 1/2 обозначают концы входной линии, а не остановки транспорта.
"""

from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from relocant_media.services.self_authored_card import (
    CARD_HEIGHT,
    CARD_WIDTH,
    _chunk,
    _circle,
    _rect,
)

MAP_WIDTH = CARD_WIDTH
MAP_HEIGHT = CARD_HEIGHT
MAX_POINTS = 1000
EARTH_RADIUS_METERS = 6_371_008.8
_BACKGROUND = (245, 247, 249)
_INK = (27, 44, 63)
_HALO = (255, 255, 255)
_ROUTE = (18, 92, 180)
_END = (183, 65, 25)
_LEFT, _RIGHT, _TOP, _BOTTOM = 96, 1050, 82, 470

# Небольшой встроенный растр исключает зависимость от установленных шрифтов.
_GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    "~": ("00000", "00000", "01001", "10110", "00000", "00000", "00000"),
    "м": ("00000", "00000", "10001", "11011", "10101", "10001", "10001"),
    "к": ("00000", "00000", "10001", "10010", "11100", "10010", "10001"),
    "С": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    " ": ("00000",) * 7,
}


@dataclass(frozen=True)
class _Layout:
    points: tuple[tuple[int, int], ...]
    pixels_per_meter: float
    scale_meters: float


def _coordinate(value: str, bound: int) -> float:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("Координата должна быть короткой Decimal-строкой")
    if value != value.strip() or "_" in value:
        raise ValueError("Недопустимый формат координаты")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Некорректная координата") from error
    if not number.is_finite() or not -bound <= number <= bound:
        raise ValueError("Координата вне разрешённой области")
    return float(number)


def _project(line: list[list[str]]) -> tuple[tuple[float, float], ...]:
    if not isinstance(line, list) or not 2 <= len(line) <= MAX_POINTS:
        raise ValueError("Нужно от 2 до 1000 точек линии")
    points = []
    for point in line:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("Точка должна иметь вид [широта, долгота]")
        points.append((_coordinate(point[0], 85), _coordinate(point[1], 180)))
    low, high = min(p[0] for p in points), max(p[0] for p in points)
    latitude = (low + high) / 2
    cosine = math.cos(math.radians(latitude))
    if any(abs(math.cos(math.radians(edge)) / cosine - 1) > 0.02 for edge in (low, high)):
        raise ValueError("Перепад широт слишком велик для локального масштаба")
    # Разрыв ±180° не превращает короткий участок в линию через весь мир.
    longitude = points[0][1]
    projected = tuple(
        (
            EARTH_RADIUS_METERS * math.radians((lng - longitude + 180) % 360 - 180) * cosine,
            EARTH_RADIUS_METERS * math.radians(lat - latitude),
        )
        for lat, lng in points
    )
    width = max(p[0] for p in projected) - min(p[0] for p in projected)
    height = max(p[1] for p in projected) - min(p[1] for p in projected)
    if not 1 <= math.hypot(width, height) <= 100_000:
        raise ValueError("Линия вырождена или выходит за пределы локальной схемы")
    return projected


def _layout(line: list[list[str]]) -> _Layout:
    projected = _project(line)
    low_x, high_x = min(p[0] for p in projected), max(p[0] for p in projected)
    low_y, high_y = min(p[1] for p in projected), max(p[1] for p in projected)
    width, height = high_x - low_x, high_y - low_y
    scale = min(
        (_RIGHT - _LEFT) / width if width else math.inf,
        (_BOTTOM - _TOP) / height if height else math.inf,
    )
    center_x, center_y = (_LEFT + _RIGHT) / 2, (_TOP + _BOTTOM) / 2
    points = tuple(
        (
            round(center_x + (x - (low_x + high_x) / 2) * scale),
            round(center_y - (y - (low_y + high_y) / 2) * scale),
        )
        for x, y in projected
    )
    target = 240 / scale
    unit = 10 ** math.floor(math.log10(target))
    scale_meters = max(step * unit for step in (1, 2, 5) if step * unit <= target)
    return _Layout(points, scale, scale_meters)


def _runs(points: tuple[tuple[int, int], ...]) -> list[tuple[int, int, int]]:
    """Точный пиксельный путь, объединённый в горизонтальные отрезки.

    Объединение не меняет геометрию и ограничивает повторную работу на пересечениях.
    После него толстый штрих рисуется строками, а не кругом на каждый пиксель.
    """
    pixels: set[int] = set()
    for start, finish in zip(points, points[1:], strict=False):
        x, y = start
        end_x, end_y = finish
        dx, dy = abs(end_x - x), -abs(end_y - y)
        step_x, step_y = (1 if x < end_x else -1), (1 if y < end_y else -1)
        error = dx + dy
        while True:
            pixels.add(y * MAP_WIDTH + x)
            if (x, y) == finish:
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x += step_x
            if doubled <= dx:
                error += dx
                y += step_y
    runs: list[tuple[int, int, int]] = []
    for offset in sorted(pixels):
        y, x = divmod(offset, MAP_WIDTH)
        if runs and runs[-1][0] == y and runs[-1][2] + 1 == x:
            row, left, _ = runs[-1]
            runs[-1] = row, left, x
        else:
            runs.append((y, x, x))
    return runs


def _stroke(
    canvas: bytearray,
    runs: list[tuple[int, int, int]],
    radius: int,
    color: tuple[int, int, int],
) -> None:
    for delta_y in range(-radius, radius + 1):
        span = math.isqrt(radius * radius - delta_y * delta_y)
        for y, left, right in runs:
            _rect(canvas, left - span, y + delta_y, right + span + 1, y + delta_y + 1, color)


def _text(
    canvas: bytearray,
    text: str,
    x: int,
    y: int,
    color: tuple[int, int, int] = _INK,
    scale: int = 3,
) -> None:
    for character in text:
        for row, bits in enumerate(_GLYPHS[character]):
            for column, pixel in enumerate(bits):
                if pixel == "1":
                    left, top = x + column * scale, y + row * scale
                    _rect(canvas, left, top, left + scale, top + scale, color)
        x += 6 * scale


def _endpoints(canvas: bytearray, points: tuple[tuple[int, int], ...]) -> None:
    first, last = points[0], points[-1]
    if first == last:
        # Замкнутая невырожденная линия: оба конца в одной исходной точке.
        x, y = first
        _circle(canvas, x, y, 25, _HALO)
        _circle(canvas, x, y, 22, _INK)
        for row in range(-19, 20):
            span = math.isqrt(19 * 19 - row * row)
            _rect(canvas, x - span, y + row, x, y + row + 1, _ROUTE)
            _rect(canvas, x, y + row, x + span + 1, y + row + 1, _END)
        _text(canvas, "1", x - 13, y - 7, _HALO, 2)
        _text(canvas, "2", x + 2, y - 7, _HALO, 2)
        return
    x, y = first
    _circle(canvas, x, y, 22, _HALO)
    _circle(canvas, x, y, 19, _INK)
    _circle(canvas, x, y, 16, _ROUTE)
    _text(canvas, "1", x - 5, y - 7, _HALO, 2)
    x, y = last
    _rect(canvas, x - 21, y - 21, x + 22, y + 22, _HALO)
    _rect(canvas, x - 18, y - 18, x + 19, y + 19, _INK)
    _rect(canvas, x - 15, y - 15, x + 16, y + 16, _END)
    _text(canvas, "2", x - 5, y - 7, _HALO, 2)


def render_transport_map(line: list[list[str]]) -> bytes:
    """PNG 1200×630; вход — 2..1000 пар Decimal-строк [lat, lng].

    Широта ±85°, долгота ±180°; диагональ области от 1 м до 100 км.
    Изменение долготного масштаба по широте ограничено 2%. Неверные данные
    вызывают ValueError; координаты не обрезаются и не достраиваются догадкой.
    """
    layout = _layout(line)
    canvas = bytearray(bytes(_BACKGROUND) * (MAP_WIDTH * MAP_HEIGHT))
    runs = _runs(layout.points)
    _stroke(canvas, runs, 11, _HALO)
    _stroke(canvas, runs, 8, _INK)
    _stroke(canvas, runs, 5, _ROUTE)
    _endpoints(canvas, layout.points)

    # Север сверху независимо от направления исходной линии.
    _text(canvas, "С", 1122, 32)
    _rect(canvas, 1127, 77, 1132, 126, _INK)
    for row in range(21):
        _rect(canvas, 1129 - row // 2, 61 + row, 1130 + row // 2, 62 + row, _INK)

    length = round(layout.scale_meters * layout.pixels_per_meter)
    _rect(canvas, 96, 558, 97 + length, 563, _INK)
    for x in (96, 96 + length):
        _rect(canvas, x - 2, 549, x + 3, 572, _INK)
    if layout.scale_meters >= 1000:
        label = f"~ {layout.scale_meters / 1000:g} км"
    else:
        label = f"~ {layout.scale_meters:g} м"
    _text(canvas, label, 96, 519)

    raw = b"".join(
        b"\x00" + canvas[y * MAP_WIDTH * 3 : (y + 1) * MAP_WIDTH * 3] for y in range(MAP_HEIGHT)
    )
    header = struct.pack(">IIBBBBB", MAP_WIDTH, MAP_HEIGHT, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level=9))
        + _chunk(b"IEND", b"")
    )
