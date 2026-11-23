"""Обновляет описание и кнопку только существующего служебного закрепа."""
from typing import Any

import httpx

from relocant_media.bot.api import (
    MAP_BUTTON_TEXT,
    TelegramAPIError,
    TelegramBotAPI,
    inline_keyboards_match,
)
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.services.pin_policy import CHANNEL


async def _description_permission(bot: TelegramBotAPI) -> str:
    try:
        identity = await bot._call("getMe", {})
        if not isinstance(identity, dict) or type(identity.get("id")) is not int:
            return "unknown"
        member = await bot._call("getChatMember", {"chat_id": CHANNEL, "user_id": identity["id"]})
    except (httpx.HTTPError, TelegramAPIError, ValueError):
        return "unknown"
    if not isinstance(member, dict):
        return "unknown"
    status = member.get("status")
    if status == "creator":
        return "granted"
    if status == "administrator":
        capability = member.get("can_change_info")
        if type(capability) is bool:
            return "granted" if capability else "denied"
        return "unknown"
    return "denied" if status in ("member", "restricted", "left", "kicked") else "unknown"


async def ensure_service_pin_map_button(
    bot: TelegramBotAPI, policy: dict[str, Any], presentation: dict[str, Any], *,
    apply: bool, update_description: bool = False,
) -> dict[str, Any]:
    if not isinstance(presentation, dict) or not isinstance(presentation.get("navigation"), dict):
        raise ValueError("invalid Belgrade channel presentation")
    button = presentation["navigation"]["map_button"]
    description = presentation["description"]
    if (
        policy["channel_id"] != CHANNEL or presentation["channel"] != "eduvbelgrad"
        or button != {"text": MAP_BUTTON_TEXT, "url": MAP_HOME_URL, "style": "success"}
        or not isinstance(description, str) or len(description.encode("utf-16-le")) // 2 > 255
        or MAP_HOME_URL not in description.split()
    ):
        raise ValueError("invalid Belgrade channel presentation")
    info = await bot._call("getChat", {"chat_id": CHANNEL})
    if not isinstance(info, dict) or info.get("id") != CHANNEL or info.get("type") != "channel":
        raise TelegramAPIError("channel presentation target mismatch")
    result: dict[str, Any] = {"owner_action_required": False, "map_button": "no_service_pin"}
    if update_description:
        result["description"] = "unchanged"
        if info.get("description") != description:
            result["description"] = "would_update"
            permission = await _description_permission(bot)
            result["description_permission"] = permission
            if permission == "denied":
                result.update(description="owner_action_required", owner_action_required=True)
            elif apply:
                try:
                    changed = await bot._call("setChatDescription", {
                        "chat_id": CHANNEL, "description": description,
                    })
                    if changed is not True:
                        raise TelegramAPIError("channel description change was not confirmed")
                    result["description"] = "updated"
                except (httpx.HTTPError, TelegramAPIError, ValueError) as error:
                    result.update(description="owner_action_required", owner_action_required=True,
                                  description_error_type=type(error).__name__)
                    if isinstance(error, httpx.HTTPStatusError):
                        result["description_http_status"] = error.response.status_code

    pin = info.get("pinned_message")
    message_id = pin.get("message_id") if isinstance(pin, dict) else None
    if (
        not isinstance(pin, dict) or type(message_id) is not int
        or message_id not in policy["service_channel_messages"]
        or any(ad.get("channel_message_id") == message_id for ad in policy["advertisements"])
    ):
        return result
    result["message_id"] = message_id
    markup = pin.get("reply_markup", {"inline_keyboard": []})
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, list) or any(not isinstance(item, dict) for item in row)
        for row in rows
    ):
        result.update(map_button="unknown_keyboard", owner_action_required=True)
        return result

    other_rows = [[item for item in row if item.get("url") != MAP_HOME_URL] for row in rows]
    updated = {"inline_keyboard": [[button], *(row for row in other_rows if row)]}
    matches = inline_keyboards_match(markup, updated)
    result["map_button"] = "unchanged" if matches else "would_update"
    if apply and result["map_button"] == "would_update":
        try:
            changed = await bot.edit_inline_keyboard(CHANNEL, message_id, updated)
            result["map_button"] = "updated" if changed else "unchanged"
        except (httpx.HTTPError, TelegramAPIError, ValueError) as error:
            result.update(map_button="owner_action_required", owner_action_required=True,
                          map_button_error_type=type(error).__name__)
    return result
