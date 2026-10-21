from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from relocant_media.bot.api import PhotoUpload
from relocant_media.bot.formatting import compact_source_link
from relocant_media.bot.media_publisher import StagingMediaPublisher


def test_compaction_preserves_following_media_attribution_lines() -> None:
    raw = (
        "Заголовок\n\nТекст.\n\n"
        "Источник: https://example.test/news/123?from=official\n\n"
        "Фото: Город Белград"
    )
    visible, entities = compact_source_link(raw)
    assert visible == "Заголовок\n\nТекст.\n\nИсточник\n\nФото: Город Белград"
    assert "https://" not in visible
    assert entities[0]["url"] == "https://example.test/news/123?from=official"
    assert entities[0]["length"] == len("Источник".encode("utf-16-le")) // 2


class FakeMediaBot:
    def __init__(self) -> None:
        self.single: list[
            tuple[str, str, tuple[Mapping[str, object], ...]]
        ] = []
        self.album: list[
            tuple[str, str, tuple[Mapping[str, object], ...]]
        ] = []

    async def send_photo(
        self,
        chat_id: int | str,
        photo: Path | PhotoUpload,
        caption: str = "",
        caption_entities: Sequence[Mapping[str, object]] = (),
    ) -> int:
        del photo
        self.single.append((str(chat_id), caption, tuple(caption_entities)))
        return 1

    async def send_media_group(
        self,
        chat_id: int | str,
        photos: Sequence[Path | PhotoUpload],
        caption: str = "",
        caption_entities: Sequence[Mapping[str, object]] = (),
    ) -> list[int]:
        self.album.append((str(chat_id), caption, tuple(caption_entities)))
        return list(range(1, len(photos) + 1))


@pytest.mark.asyncio
async def test_media_publisher_hides_source_url_for_single_and_album() -> None:
    bot = FakeMediaBot()
    publisher = StagingMediaPublisher(bot, "-1001", "-1002")
    upload = PhotoUpload("photo.png", b"\x89PNG\r\n\x1a\npayload")
    caption = "Новость\n\nИсточник: https://example.test/long/source/path"

    assert await publisher.publish(caption, [upload]) == [1]
    assert await publisher.publish(caption, [upload, upload]) == [1, 2]

    for _chat_id, visible, entities in (*bot.single, *bot.album):
        assert visible == "Новость\n\nИсточник"
        assert "https://" not in visible
        assert entities == (
            {
                "type": "text_link",
                "offset": len("Новость\n\n".encode("utf-16-le")) // 2,
                "length": len("Источник".encode("utf-16-le")) // 2,
                "url": "https://example.test/long/source/path",
            },
        )
