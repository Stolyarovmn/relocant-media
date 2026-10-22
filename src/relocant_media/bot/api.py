import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx

from relocant_media.bot.formatting import compact_source_link
from relocant_media.bot.updates import ALLOWED_UPDATE_TYPES


class TelegramAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class PhotoUpload:
    file_name: str
    data: bytes


class TelegramBot(Protocol):
    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        entities: Sequence[Mapping[str, object]] = (),
    ) -> int: ...

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
        return self._result(method, response)

    @staticmethod
    def _result(method: str, response: httpx.Response) -> object:
        response.raise_for_status()
        raw: object = response.json()
        if not isinstance(raw, dict):
            raise TelegramAPIError(f"Telegram {method} returned a non-object response")
        data = cast(dict[str, object], raw)
        if data.get("ok") is not True:
            description = data.get("description")
            raise TelegramAPIError(f"Telegram {method} failed: {description}")
        return data.get("result")

    @staticmethod
    def _photo(photo: Path | PhotoUpload) -> tuple[str, bytes, str]:
        if isinstance(photo, Path):
            if photo.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("photo exceeds 10 MB")
            file_name, content = photo.name, photo.read_bytes()
        elif isinstance(photo, PhotoUpload):
            file_name, content = photo.file_name, photo.data
        else:
            raise ValueError("photo must be a local Path or PhotoUpload")
        if (
            not file_name
            or any(character in file_name for character in ('/', '\\', '\r', '\n', '\x00', '"'))
            or not isinstance(content, bytes)
        ):
            raise ValueError("photo requires a safe file name and immutable bytes")
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("photo exceeds 10 MB")
        suffix = Path(file_name).suffix.lower()
        if suffix in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"):
            return file_name, content, "image/jpeg"
        if suffix == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
            return file_name, content, "image/png"
        raise ValueError("photo must be a JPEG or PNG with matching content")

    @staticmethod
    def _caption(caption: str) -> None:
        if len(caption.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("photo caption exceeds 1024 UTF-16 code units")

    @staticmethod
    def _message_id(result: object) -> int:
        if not isinstance(result, dict):
            raise TelegramAPIError("media response has no message object")
        message_id = result.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise TelegramAPIError("media response has an invalid message_id")
        return message_id

    async def send_photo(
        self,
        chat_id: int | str,
        photo: Path | PhotoUpload,
        caption: str = "",
        caption_entities: Sequence[Mapping[str, object]] = (),
    ) -> int:
        self._caption(caption)
        upload = self._photo(photo)
        data = {"chat_id": str(chat_id), "caption": caption}
        if caption_entities:
            data["caption_entities"] = json.dumps(
                list(caption_entities), ensure_ascii=False, separators=(",", ":")
            )
        response = await self._client.post(
            f"{self._base_url}/sendPhoto",
            data=data,
            files={"photo": upload},
        )
        return self._message_id(self._result("sendPhoto", response))

    async def send_media_group(
        self,
        chat_id: int | str,
        photos: Sequence[Path | PhotoUpload],
        caption: str = "",
        caption_entities: Sequence[Mapping[str, object]] = (),
    ) -> list[int]:
        self._caption(caption)
        if not 2 <= len(photos) <= 10:
            raise ValueError("photo album requires 2 to 10 photos")
        files = {f"photo{index}": self._photo(path) for index, path in enumerate(photos)}
        media: list[dict[str, object]] = []
        for index in range(len(photos)):
            item: dict[str, object] = {"type": "photo", "media": f"attach://photo{index}"}
            if index == 0:
                item["caption"] = caption
                if caption_entities:
                    item["caption_entities"] = list(caption_entities)
            media.append(item)
        response = await self._client.post(
            f"{self._base_url}/sendMediaGroup",
            data={"chat_id": str(chat_id), "media": json.dumps(media, ensure_ascii=False)},
            files=files,
        )
        result = self._result("sendMediaGroup", response)
        if not isinstance(result, list) or len(result) != len(photos):
            raise TelegramAPIError("sendMediaGroup returned an incomplete album")
        ids = [self._message_id(item) for item in result]
        if len(set(ids)) != len(ids):
            raise TelegramAPIError("sendMediaGroup returned duplicate message IDs")
        return ids

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        entities: Sequence[Mapping[str, object]] = (),
    ) -> int:
        visible, source_entities = compact_source_link(text)
        link_preview_options: dict[str, object] = {"is_disabled": True}
        if visible != text:
            if entities:
                raise ValueError(
                    "raw source links cannot be combined with precomputed Telegram entities"
                )
            text = visible
            entities = source_entities
            source_url = source_entities[0]["url"]
            if not isinstance(source_url, str):
                raise ValueError("source link entity requires a URL")
            link_preview_options = {
                "url": source_url,
                "prefer_large_media": True,
                "show_above_text": True,
            }
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": link_preview_options,
        }
        if entities:
            payload["entities"] = list(entities)
        result = await self._call("sendMessage", payload)
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
