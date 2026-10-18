import pytest

from relocant_media.bot.publisher import (
    ProductionTargetBlockedError,
    StagingTelegramPublisher,
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int | str, str]] = []

    async def send_message(self, chat_id: int | str, text: str) -> int:
        self.sent.append((chat_id, text))
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
    assert bot.sent == [("-100-staging", "Тестовый пост")]


def test_staging_publisher_rejects_production_target() -> None:
    with pytest.raises(ProductionTargetBlockedError):
        StagingTelegramPublisher(FakeBot(), "-100-same", "-100-same")
