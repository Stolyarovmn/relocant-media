"""Общий транспорт проверенной карты для автоматической и одобренной владельцем доставки."""

import hashlib
from dataclasses import dataclass
from typing import Protocol, cast

from relocant_media.ai.package import EditorialPackage
from relocant_media.bot.api import PhotoUpload, TelegramBotAPI
from relocant_media.bot.publisher import Publisher
from relocant_media.services.production_media import PreparedProductionMedia
from relocant_media.services.transport_map import MAP_MODE
from relocant_media.services.transport_map_image import render_transport_map
from relocant_media.services.utility_map import UTILITY_MAP_MODE


class RichMessageBot(Protocol):
    async def send_rich_message(
        self, chat_id: int | str, html: str, *, photo: PhotoUpload | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class PreparedNativeMessage:
    html: str
    photo: PhotoUpload | None = None
    image_sha256: str | None = None


def prepare_native_transport(
    package: EditorialPackage, publisher: Publisher, media: PreparedProductionMedia | None,
    *, editorial_text: str,
) -> tuple[RichMessageBot, PreparedNativeMessage] | None:
    native = package.presentation.native_map if package.presentation is not None else None
    if native is None:
        return None
    if media is not None:
        raise ValueError("native map and attached media require a combined review")
    expected_text = (f"{package.outcome.draft.title}\n\n{package.outcome.draft.body}"
                     f"\n\nИсточник: {native['point']['source_url']}")
    if expected_text != editorial_text:
        raise ValueError("native map cannot drop delivery-specific text")
    bot = getattr(publisher, "bot", None)
    if bot is None or not callable(getattr(bot, "send_rich_message", None)):
        raise ValueError("production publisher does not support reviewed native map")
    html = native["message_html"]
    point = native["point"]
    geometry = point.get("transport_geometry")
    line = geometry.get("line") if point.get("mode") == MAP_MODE else None
    filename = "transport-map.png"
    if point.get("mode") == UTILITY_MAP_MODE and point.get("image_sha256") is not None:
        line = point["geography"]["lines"][0]["coordinates"]
        filename = "outage-map.png"
    photo = None
    image_sha256 = None
    if line:
        content = render_transport_map(line)
        image_sha256 = hashlib.sha256(content).hexdigest()
        if point.get("image_sha256") != image_sha256:
            raise ValueError("map image does not match reviewed bytes")
        photo = PhotoUpload(filename, content)
        # Все локальные ошибки вложения должны возникнуть до долговечной отметки sending.
        if TelegramBotAPI._photo(photo)[2] != "image/png":
            raise ValueError("map image must be PNG")
    elif point.get("image_sha256") is not None:
        raise ValueError("reviewed map image requires a nonempty source line")
    references = html.count("tg://photo?id=transport_map")
    if references != (1 if photo is not None else 0):
        raise ValueError("map image and reviewed HTML do not match")
    return cast(RichMessageBot, bot), PreparedNativeMessage(html, photo, image_sha256)


def rich_receipt(
    prepared: PreparedNativeMessage | str, *, state: str, message_id: int | None = None,
) -> dict[str, object]:
    value = PreparedNativeMessage(prepared) if isinstance(prepared, str) else prepared
    receipt: dict[str, object] = {
        "state": state, "message_html_sha256": hashlib.sha256(value.html.encode()).hexdigest(),
        "planned_message_count": 1, "message_ids": [message_id] if message_id else []}
    if value.image_sha256 is not None:
        receipt["image_sha256"] = value.image_sha256
    return receipt


async def send_native_message(
    bot: RichMessageBot, target: int, prepared: PreparedNativeMessage | str,
) -> int:
    """Одна попытка; неопределённый результат обрабатывает общая долговечная квитанция."""
    value = PreparedNativeMessage(prepared) if isinstance(prepared, str) else prepared
    if value.photo is None:
        message_id = await bot.send_rich_message(target, value.html)
    else:
        message_id = await bot.send_rich_message(target, value.html, photo=value.photo)
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("rich publisher returned an invalid receipt")
    return message_id
