"""Собственная карточка отключения: вид работ, район и время крупно.

Запасной вариант для постов без подходящего превью (#336). Текст карточки берётся
только из уже проверенного расписания native_map и проходит те же два независимых
media-review, что и остальные вложения. Сторонние изображения и фрагмент карты
в карточку не попадают.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from relocant_media.services.native_map import NOTICE_LAYOUT
from relocant_media.services.utility_map import UTILITY_MAP_MODE

CARD_WIDTH = 1280
CARD_HEIGHT = 720
MAX_PLACES = 2

NoticeKind = Literal["water", "power", "heating"]

_HEADLINES: dict[str, str] = {
    "water": "Отключение воды",
    "power": "Отключение света",
    "heating": "Отключение отопления",
}
# Акцент вида работ совпадает с иконкой собственного фона (assets/notice-cards).
_ACCENTS: dict[str, tuple[int, int, int]] = {
    "water": (137, 215, 243),
    "power": (255, 214, 102),
    "heating": (255, 160, 122),
}
_WHITE = (255, 255, 255)
# Источник однозначно задаёт вид работ; общий городской сайт — только по разделу URL.
_SOURCE_KINDS: dict[str, NoticeKind] = {
    "bvk_outage_news": "water",
    "bvk_planned_works": "water",
    "eds_belgrade_outages_today": "power",
    "eds_belgrade_outages_tomorrow": "power",
    "eds_belgrade_outages_day2": "power",
    "eds_belgrade_outages_day3": "power",
    "beoelektrane_network_works": "heating",
}
_CITY_URL_KINDS: tuple[tuple[str, NoticeKind], ...] = (
    ("/vodovod-i-kanalizacija/", "water"),
    ("/elektricna-energija/", "power"),
    ("/grejanje/", "heating"),
)


class NoticeCardText(BaseModel):
    """Весь видимый текст карточки; сохраняется в generation.json для ревью."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: NoticeKind
    headline: str = Field(min_length=1, max_length=40)
    places: str = Field(min_length=1, max_length=80)
    when: str = Field(min_length=1, max_length=60)

    def lines(self) -> tuple[str, str, str]:
        return self.headline, self.places, self.when


def _kind(point: Mapping[str, Any]) -> NoticeKind | None:
    source_id = point.get("source_id")
    if isinstance(source_id, str) and source_id in _SOURCE_KINDS:
        return _SOURCE_KINDS[source_id]
    url = point.get("source_url")
    if isinstance(url, str):
        for fragment, kind in _CITY_URL_KINDS:
            if fragment in url:
                return kind
    return None


def _more(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    word = (many if 11 <= tail <= 14 else one if count % 10 == 1
            else few if 2 <= count % 10 <= 4 else many)
    return f" и ещё {count} {word}"


def _municipality(entry: str) -> str | None:
    head, separator, tail = entry.rpartition(" · ")
    return tail.strip() or None if separator and head else None


def notice_card_text(native_map: Mapping[str, Any] | None) -> NoticeCardText | None:
    """Карточка только для объявления об отключении с проверенным расписанием."""
    if not native_map or native_map.get("layout") != NOTICE_LAYOUT:
        return None
    point = native_map.get("point")
    if not isinstance(point, Mapping) or point.get("mode") != UTILITY_MAP_MODE:
        return None
    kind = _kind(point)
    schedule = native_map.get("schedule")
    if kind is None or not isinstance(schedule, list) or not schedule:
        return None
    windows = [section.get("when") for section in schedule if isinstance(section, Mapping)]
    if not windows or not all(isinstance(value, str) and value for value in windows):
        return None
    places: list[str] = []
    for section in schedule:
        for entry in (*section.get("items", ()), *section.get("areas", ())):
            name = _municipality(entry) if isinstance(entry, str) else None
            if name and name not in places:
                places.append(name)
    if not places:
        return None
    shown = ", ".join(places[:MAX_PLACES])
    if len(places) > MAX_PLACES:
        shown += _more(len(places) - MAX_PLACES, "район", "района", "районов")
    when = str(windows[0])
    if len(windows) > 1:
        when += _more(len(windows) - 1, "интервал", "интервала", "интервалов")
    if len(shown) > 80 or len(when) > 60:
        return None
    return NoticeCardText(kind=kind, headline=_HEADLINES[kind], places=shown, when=when)


@cache
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    path = files("relocant_media.assets").joinpath(f"fonts/Inter-{weight}.otf")
    return ImageFont.truetype(io.BytesIO(path.read_bytes()), size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, weight: str, size: int, width: int,
          max_lines: int) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Самый крупный кегль, при котором текст по словам укладывается в max_lines строк."""
    while size >= 28:
        font = _font(weight, size)
        head, separator, tail = text.partition(" и ещё ")
        if separator and draw.textlength(text, font=font) > width:
            # «и ещё N …» переносится целиком, а не по случайному слову.
            lines = [head, f"и ещё {tail}"]
            if all(draw.textlength(line, font=font) <= width for line in lines):
                return font, lines
            size -= 4
            continue
        lines = []
        for word in text.split():
            candidate = f"{lines[-1]} {word}" if lines else word
            if lines and draw.textlength(candidate, font=font) <= width:
                lines[-1] = candidate
            else:
                lines.append(word)
        if len(lines) <= max_lines and all(draw.textlength(line, font=font) <= width
                                           for line in lines):
            return font, lines
        size -= 4
    raise ValueError("notice card text does not fit")


@cache
def _background(kind: str) -> Image.Image:
    path = files("relocant_media.assets").joinpath(f"notice-cards/{kind}.jpg")
    image = Image.open(io.BytesIO(path.read_bytes())).convert("RGB")
    if image.size != (CARD_WIDTH, CARD_HEIGHT):
        raise ValueError("notice card background has an unexpected size")
    return image


def render_notice_card(text: NoticeCardText) -> bytes:
    """Детерминированный PNG поверх собственного фона вида работ.

    Иконка фона занимает правую треть, текст — левые ~55 % ширины.
    """
    accent = _ACCENTS[text.kind]
    image = _background(text.kind).copy()
    draw = ImageDraw.Draw(image)
    left, width, y = 88, 640, 96
    blocks = ((text.headline, "Bold", 88, accent, 2, 28),
              (text.places, "Bold", 60, _WHITE, 2, 22),
              (text.when, "Medium", 48, _WHITE, 2, 0))
    for value, weight, size, colour, max_lines, gap in blocks:
        font, lines = _wrap(draw, value, weight, size, width, max_lines)
        line_height = round(font.size * 1.18)
        for line in lines:
            draw.text((left, y), line, fill=colour, font=font)
            y += line_height
        y += gap
    draw.text((left, CARD_HEIGHT - 96), "Еду в Белград · время Белграда", fill=accent,
              font=_font("Medium", 30))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
