import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import httpx

from relocant_media.bot.formatting import (
    compact_source_link,
    formatted_text_units,
    source_link_entities,
)
from relocant_media.bot.updates import ALLOWED_UPDATE_TYPES
from relocant_media.domain.map_links import is_map_event_url
from relocant_media.domain.source_locations import is_public_https_url


class TelegramAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class PhotoUpload:
    file_name: str
    data: bytes


def map_reply_markup(url: str) -> dict[str, object]:
    if not is_map_event_url(url):
        raise ValueError("map button URL is not approved")
    return {"inline_keyboard": [[{"text": "Посмотреть на карте", "url": url}]]}


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

    @staticmethod
    def _source_preview_url(
        text: str,
        entities: Sequence[Mapping[str, object]],
    ) -> str | None:
        if len(entities) != 1:
            return None
        candidate = entities[0]
        url = candidate.get("url")
        if not isinstance(url, str):
            return None
        try:
            expected = source_link_entities(text, url)
        except ValueError:
            return None
        if len(expected) == 1 and dict(candidate) == expected[0]:
            return url
        return None

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

    async def send_rich_message(
        self, chat_id: int | str, html: str, *, photo: PhotoUpload | None = None,
        map_url: str | None = None, reply_to: int | None = None,
    ) -> int:
        """Отправить уже проверенный rich HTML один раз, без повторов или fallback."""
        if not html.strip():
            raise ValueError("rich message HTML is required")
        reply_markup = map_reply_markup(map_url) if map_url is not None else None
        reply = self._reply(reply_to)
        references = [media_id for media_id in ("transport_map", "notice_cover")
                      for _ in range(html.count(f"tg://photo?id={media_id}"))]
        if photo is None:
            if references:
                raise ValueError("rich message references a missing photo")
            payload: dict[str, object] = {
                "chat_id": chat_id, "rich_message": {"html": html},
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            if reply:
                payload["reply_parameters"] = reply
            result = await self._call("sendRichMessage", payload)
        else:
            if len(references) != 1:
                raise ValueError("rich message must reference its photo exactly once")
            media_id = references[0]
            upload = self._photo(photo)
            if media_id == "transport_map" and upload[2] != "image/png":
                raise ValueError("transport map must be PNG")
            rich_message: dict[str, object] = {"html": html, "media": [{
                "id": media_id, "media": {
                    "type": "photo", "media": f"attach://{media_id}"},
            }]}
            if media_id == "notice_cover":
                rich_message["skip_entity_detection"] = True
            data = {"chat_id": str(chat_id), "rich_message": json.dumps(
                rich_message, ensure_ascii=False, separators=(",", ":"))}
            if reply_markup is not None:
                data["reply_markup"] = json.dumps(
                    reply_markup, ensure_ascii=False, separators=(",", ":"),
                )
            if reply:
                data["reply_parameters"] = json.dumps(reply)
            response = await self._client.post(
                f"{self._base_url}/sendRichMessage",
                data=data,
                files={media_id: upload},
            )
            result = self._result("sendRichMessage", response)
        return self._message_id(result)

    async def edit_map_button(self, chat_id: int, message_id: int, url: str) -> bool:
        """Изменить только клавиатуру существующего поста и сверить ответ Telegram."""
        markup = map_reply_markup(url)
        try:
            result = await self._call("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id, "reply_markup": markup,
            })
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 400:
                body = error.response.json()
                description = body.get("description") if isinstance(body, dict) else None
                if isinstance(description, str) and description.startswith(
                    "Bad Request: message is not modified"
                ):
                    return False
            raise
        if (not isinstance(result, dict) or self._message_id(result) != message_id
                or not isinstance(result.get("chat"), dict) or result["chat"].get("id") != chat_id
                or result.get("reply_markup") != markup):
            raise TelegramAPIError("edited map button receipt does not match the requested post")
        return True

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
        if visible != text:
            if entities:
                raise ValueError(
                    "raw source links cannot be combined with precomputed Telegram entities"
                )
            text = visible
            entities = source_entities
        source_url = self._source_preview_url(text, entities)
        link_preview_options: dict[str, object]
        if source_url is None:
            link_preview_options = {"is_disabled": True}
        else:
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

    async def send_notice_message(
        self, chat_id: int | str, html: str, *, source_url: str, map_url: str | None = None,
        reply_to: int | None = None,
    ) -> int:
        if not is_public_https_url(source_url):
            raise ValueError("notice preview requires a public HTTPS source")
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": html,
            "parse_mode": "HTML",
            "link_preview_options": {"url": source_url, "show_above_text": True},
        }
        if map_url is not None:
            payload["reply_markup"] = map_reply_markup(map_url)
        if reply := self._reply(reply_to):
            payload["reply_parameters"] = reply
        return self._message_id(await self._call("sendMessage", payload))

    @staticmethod
    def _reply(reply_to: int | None) -> dict[str, object]:
        if reply_to is None:
            return {}
        if type(reply_to) is not int or reply_to <= 0:
            raise ValueError("reply requires an existing channel message ID")
        return {"message_id": reply_to, "allow_sending_without_reply": True}

    async def send_digest_message(
        self, chat_id: int, html: str, *, map_url: str | None = None,
        photo: Path | None = None, reply_to: int | None = None,
    ) -> int:
        payload: dict[str, object] = {
            "chat_id": chat_id, "parse_mode": "HTML", "disable_notification": True,
        }
        if map_url is not None:
            payload["reply_markup"] = map_reply_markup(map_url)
        if reply := self._reply(reply_to):
            payload["reply_parameters"] = reply
        if photo is not None:
            if formatted_text_units(html) > 1024:
                raise ValueError("digest exceeds photo caption limit")
            form = {key: value if isinstance(value, str) else json.dumps(value)
                    for key, value in payload.items()}
            form["caption"] = html
            response = await self._client.post(f"{self._base_url}/sendPhoto", data=form,
                                               files={"photo": self._photo(photo)})
            return self._message_id(self._result("sendPhoto", response))
        payload.update(text=html, link_preview_options={"is_disabled": True})
        return self._message_id(await self._call("sendMessage", payload))

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
