from collections.abc import Mapping
from typing import Protocol, cast

import httpx

from relocant_media.bot.updates import ALLOWED_UPDATE_TYPES


class TelegramAPIError(RuntimeError):
    pass


class TelegramBot(Protocol):
    async def send_message(self, chat_id: int | str, text: str) -> int: ...

    async def delete_message(self, chat_id: int | str, message_id: int) -> None: ...

    async def restrict_member(self, chat_id: int | str, user_id: int) -> None: ...


class TelegramBotAPI:
    def __init__(self, token: str, client: httpx.AsyncClient) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self._client = client
        self._base_url = f"https://api.telegram.org/bot{token}"

    async def _call(self, method: str, payload: Mapping[str, object]) -> object:
        response = await self._client.post(f"{self._base_url}/{method}", json=dict(payload))
        response.raise_for_status()
        raw: object = response.json()
        if not isinstance(raw, dict):
            raise TelegramAPIError(f"Telegram {method} returned a non-object response")
        data = cast(dict[str, object], raw)
        if data.get("ok") is not True:
            description = data.get("description")
            raise TelegramAPIError(f"Telegram {method} failed: {description}")
        return data.get("result")

    async def send_message(self, chat_id: int | str, text: str) -> int:
        result = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        if not isinstance(result, dict):
            raise TelegramAPIError("sendMessage returned no message object")
        message_id = result.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            raise TelegramAPIError("sendMessage returned an invalid message_id")
        return message_id

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        result = await self._call(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )
        if result is not True:
            raise TelegramAPIError("deleteMessage did not return true")

    async def restrict_member(self, chat_id: int | str, user_id: int) -> None:
        result = await self._call(
            "restrictChatMember",
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "permissions": {"can_send_messages": False},
                "use_independent_chat_permissions": True,
            },
        )
        if result is not True:
            raise TelegramAPIError("restrictChatMember did not return true")

    async def set_webhook(self, url: str, secret_token: str) -> None:
        if not url.startswith("https://"):
            raise ValueError("Telegram webhook URL must use HTTPS")
        if not secret_token:
            raise ValueError("Telegram webhook secret token is required")
        result = await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": list(ALLOWED_UPDATE_TYPES),
                "drop_pending_updates": False,
            },
        )
        if result is not True:
            raise TelegramAPIError("setWebhook did not return true")
