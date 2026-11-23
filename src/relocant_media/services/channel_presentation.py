"""Шапка канала и кнопка главной карты в служебных закрепах.

Модуль не отправляет новых сообщений, не закрепляет и не снимает закрепы.
Он меняет только описание канала, текст согласованного навигационного поста
и клавиатуру служебных постов, перечисленных в ``pin-policy.json``.
"""
from pathlib import Path
from typing import Any

import httpx
import yaml

from relocant_media.bot.api import (
    MAP_BUTTON_STYLE,
    TelegramAPIError,
    TelegramBotAPI,
    map_home_reply_markup,
    same_reply_markup,
)
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.services.pin_policy import CHANNEL, DISCUSSION

DESCRIPTION_LIMIT = 255
_NOT_MODIFIED = "Bad Request: message is not modified"


def load_presentation(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("channel presentation must be a mapping")
    description = data.get("description")
    navigation = data.get("navigation")
    if (not isinstance(description, str) or not 0 < len(description) <= DESCRIPTION_LIMIT
            or MAP_HOME_URL.removeprefix("https://") not in description):
        raise ValueError("channel description must link the map and fit Telegram limits")
    if (not isinstance(navigation, dict) or type(navigation.get("message_id")) is not int
            or not isinstance(navigation.get("html"), str) or not navigation["html"].strip()):
        raise ValueError("navigation post is not configured")
    button = navigation.get("map_button")
    expected = map_home_reply_markup()["inline_keyboard"][0][0]  # type: ignore[index]
    if not isinstance(button, dict) or any(button.get(key) != expected[key]
                                           for key in ("text", "url", "style")):
        raise ValueError("navigation map button must match the shared home map button")
    return data


def _check_policy(policy: dict[str, Any]) -> list[int]:
    if policy.get("channel_id") != CHANNEL or policy.get("discussion_id") != DISCUSSION:
        raise ValueError("pin policy targets are not approved")
    service = policy.get("service_channel_messages")
    if not isinstance(service, list) or not all(type(i) is int and i > 0 for i in service):
        raise ValueError("service channel messages are not configured")
    return service


def has_home_map_button(markup: object) -> bool:
    """Есть ли среди кнопок зелёная кнопка главной карты."""
    if not isinstance(markup, dict) or not isinstance(markup.get("inline_keyboard"), list):
        return False
    return any(isinstance(button, dict) and button.get("url") == MAP_HOME_URL
               and button.get("style", MAP_BUTTON_STYLE) == MAP_BUTTON_STYLE
               for row in markup["inline_keyboard"] if isinstance(row, list)
               for button in row)


def _not_modified(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 400:
        try:
            body = error.response.json()
        except ValueError:
            return False
        description = body.get("description") if isinstance(body, dict) else None
        return isinstance(description, str) and description.startswith(_NOT_MODIFIED)
    return isinstance(error, TelegramAPIError) and _NOT_MODIFIED in str(error)


async def _edit(bot: TelegramBotAPI, method: str, payload: dict[str, Any]) -> str:
    markup = map_home_reply_markup()
    try:
        result = await bot._call(method, {**payload, "chat_id": CHANNEL, "reply_markup": markup})
    except Exception as error:
        if _not_modified(error):
            return "unchanged"
        raise
    if (not isinstance(result, dict) or result.get("message_id") != payload["message_id"]
            or not isinstance(result.get("chat"), dict) or result["chat"].get("id") != CHANNEL
            or not same_reply_markup(result.get("reply_markup"), markup)):
        raise TelegramAPIError(f"{method} receipt does not match the service post")
    return "edited"


async def ensure_service_pin_map_button(
    bot: TelegramBotAPI, policy: dict[str, Any],
) -> dict[str, Any]:
    """Верхний служебный закреп канала всегда несёт кнопку главной карты.

    Читается только ``getChat``; правится лишь клавиатура служебного поста.
    Новостные, рекламные и чужие закрепы не трогаются.
    """
    service = _check_policy(policy)
    result: dict[str, Any] = {"status": "skipped", "owner_action_required": False}
    try:
        info = await bot._call("getChat", {"chat_id": CHANNEL})
        pin = info.get("pinned_message") if isinstance(info, dict) else None
        message_id = pin.get("message_id") if isinstance(pin, dict) else None
        result["message_id"] = message_id
        if not isinstance(pin, dict) or message_id not in service:
            result["status"] = "top_pin_not_service"
            return result
        if has_home_map_button(pin.get("reply_markup")):
            result["status"] = "present"
            return result
        result["status"] = await _edit(bot, "editMessageReplyMarkup",
                                       {"message_id": message_id})
    except Exception as error:
        result.update(status="blocked", owner_action_required=True,
                      error_type=type(error).__name__)
    return result


async def apply_presentation(
    bot: TelegramBotAPI, policy: dict[str, Any], presentation: dict[str, Any],
) -> dict[str, Any]:
    """Применить согласованные шапку и навигацию; повторный запуск безопасен."""
    service = _check_policy(policy)
    navigation = presentation["navigation"]
    if navigation["message_id"] not in service:
        raise ValueError("navigation post must be a registered service pin")
    result: dict[str, Any] = {}
    info = await bot._call("getChat", {"chat_id": CHANNEL})
    if not isinstance(info, dict):
        raise TelegramAPIError("invalid getChat response")
    if info.get("description") == presentation["description"]:
        result["description"] = "unchanged"
    else:
        if await bot._call("setChatDescription", {
                "chat_id": CHANNEL, "description": presentation["description"]}) is not True:
            raise TelegramAPIError("setChatDescription not confirmed")
        result["description"] = "edited"
    result["navigation"] = await _edit(bot, "editMessageText", {
        "message_id": navigation["message_id"], "text": navigation["html"],
        "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
    result["service_buttons"] = {}
    for message_id in service:
        if message_id != navigation["message_id"]:
            result["service_buttons"][str(message_id)] = await _edit(
                bot, "editMessageReplyMarkup", {"message_id": message_id})
    return result
