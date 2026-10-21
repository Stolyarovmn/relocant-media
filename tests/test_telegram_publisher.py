from collections.abc import Mapping, Sequence

import pytest

from relocant_media.bot.publisher import (
    ProductionTargetBlockedError,
    StagingTelegramPublisher,
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int | str, str, tuple[Mapping[str, object], ...]]] = []

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        entities: Sequence[Mapping[str, object]] = (),
    ) -> int:
        self.sent.append((chat_id, text, tuple(entities)))
        return 77

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        del chat_id, message_id

    async def restrict_member(self, chat_id: int | str, user_id: int) -> None:
        del chat_id, user_id


@pytest.mark.asyncio
async def test_staging_publisher_targets_only_staging_channel() -> None:
    bot = FakeBot()
    publisher = StagingTelegramPublisher(bot, "-100-staging", "-100-production")

    message_id = await publisher.publish("Тестовый пост")

    assert message_id == 77
    assert bot.sent == [("-100-staging", "Тестовый пост", ())]


@pytest.mark.asyncio
async def test_staging_publisher_hides_raw_source_url_behind_source_label() -> None:
    bot = FakeBot()
    publisher = StagingTelegramPublisher(bot, "-100-staging", "-100-production")
    raw = "😀 Важная новость\n\nТекст по-русски.\n\nИсточник: https://example.test/very/long/path?q=1"

    assert await publisher.publish(raw) == 77

    chat_id, visible, entities = bot.sent[0]
    assert chat_id == "-100-staging"
    assert visible == "😀 Важная новость\n\nТекст по-русски.\n\nИсточник"
    assert "https://" not in visible
    assert entities == (
        {
            "type": "text_link",
            "offset": len("😀 Важная новость\n\nТекст по-русски.\n\n".encode("utf-16-le")) // 2,
            "length": len("Источник".encode("utf-16-le")) // 2,
            "url": "https://example.test/very/long/path?q=1",
        },
    )


@pytest.mark.asyncio
async def test_staging_publisher_rejects_ambiguous_or_private_source_links() -> None:
    publisher = StagingTelegramPublisher(FakeBot(), "-100-staging", "-100-production")
    with pytest.raises(ValueError, match="ambiguous"):
        await publisher.publish(
            "Источник: https://example.test/a\nИсточник: https://example.test/b"
        )
    with pytest.raises(ValueError, match="public HTTPS"):
        await publisher.publish("Источник: https://user:secret@example.test/a")


def test_staging_publisher_rejects_production_target() -> None:
    with pytest.raises(ProductionTargetBlockedError):
        StagingTelegramPublisher(FakeBot(), "-100-same", "-100-same")
