"""Небольшие редакционные иллюстрации для шапок объявлений."""

import struct
import zlib
from functools import cache

from relocant_media.services.self_authored_card import _chunk

_WIDTH, _HEIGHT = 800, 160
_PALETTES = {
    "water": ((18, 66, 94), (137, 215, 243)),
    "power": ((65, 42, 112), (222, 204, 255)),
    "road": ((101, 37, 44), (255, 203, 171)),
}
_SYMBOLS = {
    "water": ((400, 32), (369, 76), (362, 100), (369, 117), (384, 128), (400, 132),
              (416, 128), (431, 117), (438, 100), (431, 76)),
    "power": ((413, 28), (366, 89), (397, 89), (385, 132), (439, 68), (408, 68)),
    "road": ((379, 30), (421, 30), (466, 130), (334, 130)),
}


@cache
def render_notice_banner(kind: str) -> bytes:
    """Собственная графика категории без внешних изображений или новых сведений."""
    background, accent = _PALETTES[kind]
    shape = _SYMBOLS[kind]
    edges = tuple(zip(shape, (*shape[1:], shape[0]), strict=True))
    stripe = bytes((2 * b + a) // 3 for b, a in zip(background, accent, strict=True))
    fill = bytes(accent)
    canvas = bytearray(bytes(background) * (_WIDTH * _HEIGHT))
    for y in range(_HEIGHT):
        for x in range(_WIDTH):
            if (x < 268 or x > 532) and (x + 2 * y) % 42 < 3:
                offset = (y * _WIDTH + x) * 3
                canvas[offset:offset + 3] = stripe
        for x in range(334, 467):
            inside = sum((y1 > y) != (y2 > y)
                         and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                         for (x1, y1), (x2, y2) in edges) % 2
            if inside and not (kind == "road" and abs(x - 400) <= 4 and y % 29 < 17):
                offset = (y * _WIDTH + x) * 3
                canvas[offset:offset + 3] = fill
    stride = _WIDTH * 3
    rows = b"".join(b"\x00" + canvas[y:y + stride]
                    for y in range(0, len(canvas), stride))
    header = struct.pack(">IIBBBBB", _WIDTH, _HEIGHT, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(rows, level=9)) + _chunk(b"IEND", b""))
