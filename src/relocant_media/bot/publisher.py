from typing import Protocol

from relocant_media.bot.api import TelegramBot


class Publisher(Protocol):
    async def publish(self, text: str) -> int: ...


class ProductionTargetBlockedError(ValueError):
    pass


class StagingTelegramPublisher:
    def __init__(
        self,
        bot: TelegramBot,
        staging_channel_id: str,
        production_channel_id: str = "",
    ) -> None:
        if not staging_channel_id:
            raise ValueError("staging Telegram channel id is required")
        if production_channel_id and staging_channel_id == production_channel_id:
            raise ProductionTargetBlockedError(
                "staging channel must not be the configured production channel"
            )
        self._bot = bot
        self._channel_id = staging_channel_id

    async def publish(self, text: str) -> int:
        if not text.strip():
            raise ValueError("cannot publish an empty Telegram post")
        return await self._bot.send_message(self._channel_id, text)
