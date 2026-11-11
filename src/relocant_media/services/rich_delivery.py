"""Общий транспорт проверенной карты для автоматической и одобренной владельцем доставки."""

import hashlib
from typing import Protocol, cast

from relocant_media.ai.package import EditorialPackage
from relocant_media.bot.publisher import Publisher
from relocant_media.services.production_media import PreparedProductionMedia


class RichMessageBot(Protocol):
    async def send_rich_message(self, chat_id: int | str, html: str) -> int: ...


def prepare_native_transport(
    package: EditorialPackage, publisher: Publisher, media: PreparedProductionMedia | None,
    *, editorial_text: str,
) -> tuple[RichMessageBot, str] | None:
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
    return cast(RichMessageBot, bot), native["message_html"]


def rich_receipt(html: str, *, state: str, message_id: int | None = None) -> dict[str, object]:
    return {"state": state, "message_html_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "planned_message_count": 1, "message_ids": [message_id] if message_id else []}


async def send_native_message(bot: RichMessageBot, target: int, html: str) -> int:
    """Одна попытка; неопределённый результат обрабатывает общая долговечная квитанция."""
    message_id = await bot.send_rich_message(target, html)
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("rich publisher returned an invalid receipt")
    return message_id
