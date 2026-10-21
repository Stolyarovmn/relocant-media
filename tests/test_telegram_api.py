import json

import httpx
import pytest

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.updates import ALLOWED_UPDATE_TYPES


@pytest.mark.asyncio
async def test_send_message_uses_bot_api_and_returns_message_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        message_id = await bot.send_message("-100123", "hello")

    assert message_id == 42
    assert requests[0].url.path.endswith("/bottest-token/sendMessage")
    assert json.loads(requests[0].content) == {
        "chat_id": "-100123", "text": "hello", "link_preview_options": {"is_disabled": True},
    }


@pytest.mark.asyncio
async def test_set_webhook_sends_secret_and_explicit_allowed_updates() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        await bot.set_webhook("https://example.test/telegram/webhook", "secret")

    assert payloads == [
        {
            "url": "https://example.test/telegram/webhook",
            "secret_token": "secret",
            "allowed_updates": list(ALLOWED_UPDATE_TYPES),
            "drop_pending_updates": False,
        }
    ]
