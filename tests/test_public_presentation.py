import json

import httpx
import pytest
from test_independent_reviews import Provider, documents, package
from test_pin_policy import Bot, forward, policy

from relocant_media.ai.package import draft_sha256
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.formatting import editorial_caption
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


@pytest.mark.asyncio
async def test_regular_channel_news_uses_editorial_format_and_source_preview(monkeypatch):
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    source = "https://www.beograd.rs/vest?x=1&y=2"
    text = ("Новости & события\n\nКороткое описание.\n\n"
            "Когда: 10:00–12:30\nВход: свободный\n\n«Точная цитата»\n\n"
            f"Рубрика: Куда сходить\n\nИсточник: {source}")

    def handler(request):
        assert request.url.path.endswith("/sendMessage")
        data = json.loads(request.content)
        assert data == {
            "chat_id": -1003967014133,
            "text": ("<b>Новости &amp; события</b>\n\nКороткое описание.\n\n"
                     "<b>Когда</b>: <code>10:00–12:30</code>\n<b>Вход</b>: свободный"
                     "\n\n<blockquote>«Точная цитата»</blockquote>\n\n"
                     "<b>Рубрика</b>: Куда сходить\n\n"
                     '<a href="https://www.beograd.rs/vest?x=1&amp;y=2">Источник</a>'),
            "parse_mode": "HTML",
            "link_preview_options": {"url": source, "show_above_text": True},
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 44}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = ChannelPublisher(TelegramBotAPI("fake", client))
        assert await publisher.publish(text) == 44


def test_reviewed_photo_caption_preserves_text_and_formats_utf16_entities():
    source = "https://www.beograd.rs/vest"
    caption = f"🎭 Выставка\n\nКогда: 10:00–12:30\n\nИсточник: {source}\n\nФото: музей"
    visible, entities = editorial_caption(caption)
    assert visible == "🎭 Выставка\n\nКогда: 10:00–12:30\n\nИсточник\n\nФото: музей"
    encoded = visible.encode("utf-16-le")
    fragments = [
        (e["type"], encoded[2 * e["offset"]:2 * (e["offset"] + e["length"])].decode("utf-16-le"))
        for e in entities
    ]
    assert fragments == [
        ("text_link", "Источник"), ("bold", "🎭 Выставка"), ("bold", "Когда"),
        ("code", "10:00–12:30"),
    ]


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
