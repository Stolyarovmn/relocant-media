from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from relocant_media.bot.api import PhotoUpload
from relocant_media.bot.formatting import compact_source_link
from relocant_media.bot.publisher import ProductionTargetBlockedError


class MediaBot(Protocol):
    async def send_photo(
        self,
        chat_id: int | str,
        photo: Path | PhotoUpload,
        caption: str = "",
        caption_entities: Sequence[Mapping[str, object]] = (),
    ) -> int: ...

    async def send_media_group(
        self,
        chat_id: int | str,
        photos: Sequence[Path | PhotoUpload],
        caption: str = "",
        caption_entities: Sequence[Mapping[str, object]] = (),
    ) -> list[int]: ...


class StagingMediaPublisher:
    """Transport for already validated media; never selects a production target."""

    def __init__(
        self,
        bot: MediaBot,
        staging_channel_id: str,
        production_channel_id: str = "",
    ) -> None:
        if not staging_channel_id:
            raise ValueError("staging Telegram channel id is required")
        if production_channel_id and staging_channel_id.strip() == production_channel_id.strip():
            raise ProductionTargetBlockedError(
                "staging channel must not be the configured production channel"
            )
        self._bot = bot
        self._channel_id = staging_channel_id

    @property
    def target_id(self) -> str:
        return self._channel_id

    async def publish(self, caption: str, photos: Sequence[Path | PhotoUpload]) -> list[int]:
        if not caption.strip():
            raise ValueError("media publication requires a caption")
        visible, entities = compact_source_link(caption)
        if len(visible.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("photo caption exceeds 1024 UTF-16 code units")
        if not 1 <= len(photos) <= 10:
            raise ValueError("media publication requires 1 to 10 photos")
        if len(photos) == 1:
            return [
                await self._bot.send_photo(
                    self._channel_id,
                    photos[0],
                    visible,
                    entities,
                )
            ]
        return await self._bot.send_media_group(
            self._channel_id,
            photos,
            visible,
            entities,
        )
