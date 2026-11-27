import json
import logging
import traceback

import httpx
import pytest

from relocant_media.bot.api import PhotoUpload, TelegramAPIError, TelegramBotAPI
from relocant_media.bot.formatting import compact_source_link
from relocant_media.bot.updates import ALLOWED_UPDATE_TYPES
from relocant_media.ops import smoke


@pytest.mark.asyncio
@pytest.mark.parametrize("multipart", [False, True])
async def test_bot_http_failure_hides_token_in_exception_and_logs(caplog, multipart):
    token = "123456:test-private-bot-token"
    private_text = "private-response-marker"

    def handler(request: httpx.Request) -> httpx.Response:
        assert token in request.url.path
        return httpx.Response(
            403, json={"ok": False, "description": f"{token} {private_text}"},
            headers={"Location": str(request.url)},
        )

    caplog.set_level(logging.INFO, logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI(token, client)
        with pytest.raises(httpx.HTTPStatusError, match="HTTP 403") as caught:
            if multipart:
                await bot.send_photo(1, PhotoUpload("photo.png", b"\x89PNG\r\n\x1a\n"))
            else:
                await bot.send_message(1, "private request")

    error = caught.value
    assert error.response.status_code == 403
    assert error.response.json() == {}
    assert "location" not in error.response.headers
    assert "403" in caplog.text
    assert token not in caplog.text
    assert token not in str(error.request.url)
    assert token not in str(error.response.request.url)
    assert private_text not in "".join(traceback.format_exception(error))
    assert token not in "".join(traceback.format_exception(error))


@pytest.mark.asyncio
async def test_bot_transport_failure_preserves_type_without_original_error():
    token = "123456:test-private-bot-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout for {request.url}", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout, match="sendMessage") as caught:
            await TelegramBotAPI(token, client).send_message(1, "private request")

    assert caught.value.__context__ is None
    assert token not in str(caught.value.request.url)
    assert token not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_bot_rejection_hides_response_description_and_invalid_json(malformed):
    token = "123456:test-private-bot-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if malformed:
            return httpx.Response(200, text=f"{{invalid {token}")
        return httpx.Response(200, json={"ok": False, "description": token})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelegramAPIError, match="Telegram sendMessage") as caught:
            await TelegramBotAPI(token, client).send_message(1, "private request")

    assert caught.value.__context__ is None
    assert token not in "".join(traceback.format_exception(caught.value))


def test_telegram_smoke_cli_hides_token_in_json_and_traceback(monkeypatch, capsys):
    token = "123456:test-private-bot-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert token in request.url.path
        return httpx.Response(403, text=str(request.url))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("sys.argv", ["smoke", "--telegram"])
    monkeypatch.setattr(smoke.settings, "telegram_bot_token", token)
    monkeypatch.setattr(smoke.httpx, "Client", lambda **_: client)
    monkeypatch.setattr(smoke, "check_api", lambda *_: {})
    monkeypatch.setattr(smoke, "check_telegram", lambda c: smoke._telegram_call(c, "getMe", {}))
    with pytest.raises(SystemExit) as caught:
        smoke.main()

    assert caught.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False, "error": "Telegram getMe returned HTTP 403",
    }
    assert token not in "".join(traceback.format_exception(caught.value))


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
async def test_send_message_compacts_source_and_requests_real_large_preview() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}}, request=request)

    source = "https://official.example/very/long/path?q=1"
    raw = f"Новость\n\nТекст.\n\nИсточник: {source}"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        assert await bot.send_message("-100123", raw) == 8

    payload = json.loads(requests[0].content)
    assert payload["text"] == "Новость\n\nТекст.\n\nИсточник"
    assert "https://" not in payload["text"]
    assert payload["entities"] == [
        {
            "type": "text_link",
            "offset": len("Новость\n\nТекст.\n\n".encode("utf-16-le")) // 2,
            "length": len("Источник".encode("utf-16-le")) // 2,
            "url": source,
        }
    ]
    assert payload["link_preview_options"] == {
        "url": source,
        "prefer_large_media": True,
        "show_above_text": True,
    }


@pytest.mark.asyncio
async def test_preformatted_canonical_source_link_gets_same_real_preview() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}}, request=request)

    source = "https://official.example/news"
    text, entities = compact_source_link(f"Текст\n\nИсточник: {source}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        assert await bot.send_message("-100123", text, entities) == 9

    payload = json.loads(requests[0].content)
    assert payload["text"] == "Текст\n\nИсточник"
    assert payload["entities"] == list(entities)
    assert payload["link_preview_options"] == {
        "url": source,
        "prefer_large_media": True,
        "show_above_text": True,
    }


@pytest.mark.asyncio
async def test_send_message_rejects_raw_source_url_with_precomputed_entities() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
        bot = TelegramBotAPI("test-token", client)
        with pytest.raises(ValueError, match="precomputed"):
            await bot.send_message(
                "-100123",
                "Источник: https://official.example/news",
                ({"type": "bold", "offset": 0, "length": 8},),
            )


@pytest.mark.asyncio
async def test_send_message_passes_explicit_text_link_entities_without_forcing_preview() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}}, request=request)

    entity = {"type": "text_link", "offset": 10, "length": 8, "url": "https://example.test/news"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        assert await bot.send_message("-100123", "Текст\n\nИсточник", (entity,)) == 7

    assert json.loads(requests[0].content) == {
        "chat_id": "-100123",
        "text": "Текст\n\nИсточник",
        "link_preview_options": {"is_disabled": True},
        "entities": [entity],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_intake", [False, True])
async def test_set_webhook_sends_secret_and_explicit_allowed_updates(reader_intake: bool) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        await bot.set_webhook(
            "https://example.test/telegram/webhook", "secret", reader_intake=reader_intake,
        )

    assert payloads == [
        {
            "url": "https://example.test/telegram/webhook",
            "secret_token": "secret",
            "allowed_updates": [*ALLOWED_UPDATE_TYPES, "edited_message"]
            if reader_intake else list(ALLOWED_UPDATE_TYPES),
            "drop_pending_updates": False,
            **({"max_connections": 1} if reader_intake else {}),
        }
    ]
