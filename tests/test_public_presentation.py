import pytest
from test_independent_reviews import Provider, documents, package
from test_pin_policy import Bot, forward, policy

from relocant_media.ai.package import draft_sha256
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.domain.presentation import (
    TEST_PREFIX,
    presentation_title,
    valid_title_presentation,
)
from relocant_media.ops.server_cycle import ChannelPublisher
from relocant_media.services.pin_policy import maintain_pins


def test_public_draft_is_bound_to_two_exact_reviews(monkeypatch):
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    value = package()
    draft = value.outcome.draft.model_copy(update={
        "title": presentation_title(TEST_PREFIX + " " + value.outcome.draft.title)})
    value = value.model_copy(update={"outcome": value.outcome.model_copy(update={"draft": draft})})
    provider = Provider()
    result = review_with_repairs(value, provider, lambda: documents(value))
    assert not result.outcome.draft.title.startswith(TEST_PREFIX)
    assert all(r.draft_sha256 == draft_sha256(draft) for r in result.reviews)
    assert len(provider.calls) == 2
    assert all("владелец отменил её" in c["system_instruction"] for c in provider.calls)


@pytest.mark.asyncio
async def test_public_transport_preserves_exact_text_and_blocks_legacy(monkeypatch):
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")

    class FakeBot:
        calls = []

        async def send_message(self, chat, text):
            self.calls.append((chat, text))
            return 27

    bot = FakeBot()
    publisher = ChannelPublisher(bot)
    text = "В Белграде изменится маршрут\n\nПодтверждённый текст"
    assert await publisher.publish(text) == 27
    assert bot.calls == [(-1003967014133, text)]
    with pytest.raises(ValueError):
        await publisher.publish(TEST_PREFIX + " " + text)
    assert len(bot.calls) == 1


def test_default_and_invalid_modes_fail_closed(monkeypatch):
    monkeypatch.delenv("EDITORIAL_CHANNEL_MODE", raising=False)
    assert presentation_title("Новость") == TEST_PREFIX + " Новость"
    assert not valid_title_presentation("Новость")
    assert not valid_title_presentation(TEST_PREFIX)
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "typo")
    with pytest.raises(ValueError):
        valid_title_presentation("Новость")


@pytest.mark.asyncio
async def test_public_news_unpinned_by_receipt_unknown_and_service_preserved():
    news = forward(40, 18)
    news["text"] = "Новость без служебной пометки"
    bot = Bot(channel=[dict(message_id=90, text="Неизвестное сообщение")],
              discussion=[news, dict(message_id=3)])
    result = await maintain_pins(bot, policy(), news_message_ids={18})
    assert result["unpinned"] == [{"chat_id": -1003942640600, "message_id": 40}]
    assert result["blocked"] == [{"chat_id": -1003967014133, "reason": "unknown_pin"}]
    assert result["preserved"] == [{"chat_id": -1003942640600, "message_id": 3}]
