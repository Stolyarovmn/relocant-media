"""Собственная карточка отключения: вид работ, район и время крупно.

Запасной вариант для постов без подходящего превью (#336). Текст карточки берётся
только из проверенного разбора объявления, по которому построено расписание
native_map, и проходит те же два независимых media-review, что и остальные вложения.
Сторонние изображения и фрагмент карты в карточку не попадают.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Mapping
from functools import cache
from importlib.resources import files
from typing import Any, Literal

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.native_map import NOTICE_LAYOUT, notice_scope_places
from relocant_media.services.utility_map import UTILITY_MAP_MODE, utility_document_sha256

CARD_WIDTH = 1280
CARD_HEIGHT = 720
MAX_PLACES = 2

NoticeKind = Literal["water", "power"]

_HEADLINES: dict[str, str] = {
    "water": "Отключение воды",
    "power": "Отключение света",
}
# Акцент вида работ совпадает с иконкой собственного фона (assets/notice-cards).
_ACCENTS: dict[str, tuple[int, int, int]] = {
    "water": (137, 215, 243),
    "power": (255, 214, 102),
}
_WHITE = (255, 255, 255)
# Профильный источник не может противоречить разбору: иначе карточки нет.
_SOURCE_KINDS: dict[str, NoticeKind] = {
    "bvk_outage_news": "water",
    "bvk_planned_works": "water",
    "eds_belgrade_outages_today": "power",
    "eds_belgrade_outages_tomorrow": "power",
    "eds_belgrade_outages_day2": "power",
    "eds_belgrade_outages_day3": "power",
}
# Все 17 городских муниципалитетов Белграда в латинице и сербской кириллице.
_MUNICIPALITIES_RU: dict[str, str] = {
    name.casefold(): russian
    for russian, names in {
        "Бараево": ("Barajevo", "Барајево"),
        "Чукарица": ("Čukarica", "Чукарица"),
        "Гроцка": ("Grocka", "Гроцка"),
        "Лазаревац": ("Lazarevac", "Лазаревац"),
        "Младеновац": ("Mladenovac", "Младеновац"),
        "Нови-Београд": ("Novi Beograd", "Нови Београд"),
        "Обреновац": ("Obrenovac", "Обреновац"),
        "Палилула": ("Palilula", "Палилула"),
        "Раковица": ("Rakovica", "Раковица"),
        "Савски-Венац": ("Savski venac", "Савски венац"),
        "Сопот": ("Sopot", "Сопот"),
        "Стари-Град": ("Stari grad", "Стари град"),
        "Сурчин": ("Surčin", "Сурчин"),
        "Вождовац": ("Voždovac", "Вождовац"),
        "Врачар": ("Vračar", "Врачар"),
        "Земун": ("Zemun", "Земун"),
        "Звездара": ("Zvezdara", "Звездара"),
    }.items()
    for name in names
}


CardLayout = Literal["shared_window", "per_district", "see_map"]
MAP_NOTE = "Адреса и время на карте"
MAX_DISTRICT_ROWS = 3


class NoticeCardText(BaseModel):
    """Весь видимый текст карточки; сохраняется в generation.json для ревью.

    Время показывается только там, где оно однозначно относится к району:
    общий интервал — когда он одинаков у всех перечисленных районов, иначе
    отдельная строка «район · время» или подпись «Адреса и время на карте».
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: NoticeKind
    layout: CardLayout
    headline: str = Field(min_length=1, max_length=40)
    body: tuple[str, ...] = Field(min_length=2, max_length=1 + MAX_DISTRICT_ROWS)

    def lines(self) -> tuple[str, ...]:
        return (self.headline, *self.body)


def _more(count: int, one: str, few: str, many: str) -> str:
    tail = count % 100
    word = (many if 11 <= tail <= 14 else one if count % 10 == 1
            else few if 2 <= count % 10 <= 4 else many)
    return f" и ещё {count} {word}"


def _reviewed_document(
    point: Mapping[str, Any], documents: Iterable[EvidenceDocument],
) -> EvidenceDocument | None:
    for candidate in documents:
        if (candidate.url, candidate.source_id) != (point.get("source_url"),
                                                    point.get("source_id")):
            continue
        document = EvidenceDocument.model_validate(
            candidate.model_dump(include=set(EvidenceDocument.model_fields)))
        if utility_document_sha256(document) == point.get("document_sha256"):
            return document
    return None


def notice_card_text(
    native_map: Mapping[str, Any] | None, documents: Iterable[EvidenceDocument] = (),
) -> NoticeCardText | None:
    """Карточка только из проверенного разбора объявления об отключении.

    Вид работ и районы берутся из того же разбора документа, по которому построено
    расписание `map_notice_v2`; документ сверяется с отпечатком точки карты,
    а окна — с расписанием пакета.
    """
    if not native_map or native_map.get("layout") != NOTICE_LAYOUT:
        return None
    point = native_map.get("point")
    schedule = native_map.get("schedule")
    if (not isinstance(point, Mapping) or point.get("mode") != UTILITY_MAP_MODE
            or not isinstance(schedule, list) or not schedule):
        return None
    document = _reviewed_document(point, documents)
    if document is None:
        return None
    facts = notice_scope_places(document, dict(point))
    if facts is None:
        return None
    kind, pairs = facts
    if kind not in _HEADLINES or _SOURCE_KINDS.get(document.source_id, kind) != kind:
        return None
    reviewed_windows = {section.get("when") for section in schedule
                        if isinstance(section, Mapping)}
    # Район -> окна, в которых он упомянут, в порядке разбора.
    windows_by_place: dict[str, list[str]] = {}
    for when, municipality in pairs:
        place = _MUNICIPALITIES_RU.get(municipality.casefold())
        if place is None or when not in reviewed_windows:
            return None
        windows = windows_by_place.setdefault(place, [])
        if when not in windows:
            windows.append(when)
    places = list(windows_by_place)
    all_windows = {when for windows in windows_by_place.values() for when in windows}
    dates = sorted({when.split(" · ", 1)[0] for when in all_windows},
                   key=lambda value: value.split(".")[::-1])
    headline = _HEADLINES[kind]
    notice_kind: NoticeKind = "power" if kind == "power" else "water"
    if len(all_windows) == 1:
        shown = ", ".join(places[:MAX_PLACES])
        if len(places) > MAX_PLACES:
            shown += _more(len(places) - MAX_PLACES, "район", "района", "районов")
        return NoticeCardText(kind=notice_kind, layout="shared_window", headline=headline,
                              body=(shown, next(iter(all_windows))))
    one_window_each = all(len(windows) == 1 for windows in windows_by_place.values())
    if one_window_each and len(places) <= MAX_DISTRICT_ROWS and len(dates) == 1:
        rows = tuple(f"{place} · {windows_by_place[place][0].split(' · ', 1)[1]}"
                     for place in places)
        return NoticeCardText(kind=notice_kind, layout="per_district", headline=headline,
                              body=(dates[0], *rows))
    date = dates[0] if len(dates) == 1 else f"{dates[0]} – {dates[-1]}"
    return NoticeCardText(kind=notice_kind, layout="see_map", headline=headline,
                          body=(date, MAP_NOTE))


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
    first, *rest = text.body
    # (текст, начертание, кегль, цвет, максимум строк, отступ после блока)
    blocks: list[tuple[str, str, int, tuple[int, int, int], int, int]] = [
        (text.headline, "Bold", 88, accent, 2, 28)]
    if text.layout == "shared_window":
        blocks += [(first, "Bold", 60, _WHITE, 2, 22), (rest[0], "Medium", 48, _WHITE, 2, 0)]
    elif text.layout == "per_district":
        blocks.append((first, "Medium", 48, _WHITE, 1, 14))
        blocks += [(row, "Bold", 48, _WHITE, 1, 6) for row in rest]
    else:
        blocks += [(first, "Bold", 60, _WHITE, 1, 22), (rest[0], "Medium", 48, _WHITE, 1, 0)]
    for value, weight, size, colour, max_lines, gap in blocks:
        font, lines = _wrap(draw, value, weight, size, width, max_lines)
        line_height = round(font.size * 1.18)
        for line in lines:
            draw.text((left, y), line, fill=colour, font=font)
            y += line_height
        y += gap
    if y > CARD_HEIGHT - 104:
        raise ValueError("notice card text does not fit")
    draw.text((left, CARD_HEIGHT - 96), "Еду в Белград · время Белграда", fill=accent,
              font=_font("Medium", 30))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()
