"""A bot can count channel members, but cannot read historical post views."""

from datetime import UTC, datetime
from typing import Any

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID


async def observe_audience(bot: TelegramBotAPI) -> dict[str, Any]:
    result: dict[str, Any] = dict(
        source="telegram_bot_api",
        channel_id=APPROVED_CHANNEL_ID,
        observed_at=datetime.now(UTC).isoformat(),
        subscribers=None,
        post_views=None,
        reactions=None,
        comments=None,
        forwards=None,
        ai_calls=0,
    )
    try:
        count = await bot._call("getChatMemberCount", {"chat_id": APPROVED_CHANNEL_ID})
        if type(count) is not int or count < 0:
            raise ValueError("invalid member count")
        result.update(status="observed", subscribers=count)
    except Exception as error:
        result.update(status="unavailable", error_type=type(error).__name__)
    return result
