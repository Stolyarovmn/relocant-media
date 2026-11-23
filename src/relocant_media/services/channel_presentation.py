"""Обновляет описание и кнопку только существующего служебного закрепа."""
from datetime import UTC, datetime
from typing import Any

import httpx

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI, inline_keyboards_match
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.services.pin_policy import CHANNEL, _ad_active


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
        or button != {"text": "🗺 Открыть карту", "url": MAP_HOME_URL, "style": "success"}
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
            if apply:
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


MAP_PIN_TEXT = ("🗺 Посмотреть на карте\n"
                "Перекрытия, отключения воды и света и события Белграда на одной карте.")
MAP_PIN_MARKUP = {"inline_keyboard": [[
    {"text": "🗺 Открыть карту", "url": MAP_HOME_URL, "style": "success"}]]}


def _active_ad_ids(policy: dict[str, Any], now: datetime) -> set[int]:
    return {ad["channel_message_id"] for ad in policy.get("advertisements", ())
            if isinstance(ad, dict) and _ad_active(ad, now)}


async def ensure_map_pin_on_top(
    bot: TelegramBotAPI, policy: dict[str, Any], *, apply: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Верхний закреп канала — пост карты, а во время оплаченной рекламы — реклама.

    Telegram показывает в полосе закрепа самый новый по дате закреплённый пост,
    поэтому старый пост поднять нельзя. При ``apply`` бот один раз тихо отправляет
    новый пост карты и закрепляет его без звука; прежний пост карты лишь
    открепляется. Новый ID нужно внести в ``pin-policy.json``.
    """
    if policy.get("channel_id") != CHANNEL:
        raise ValueError("pin policy targets are not approved")
    now = now or datetime.now(UTC)
    current = policy.get("map_channel_message")
    info = await bot._call("getChat", {"chat_id": CHANNEL})
    if not isinstance(info, dict) or info.get("id") != CHANNEL:
        raise TelegramAPIError("channel target mismatch")
    pin = info.get("pinned_message")
    top = pin.get("message_id") if isinstance(pin, dict) else None
    result: dict[str, Any] = {"top_message_id": top, "owner_action_required": False}
    if type(current) is int and top == current:
        result["map_pin"] = "on_top"
        return result
    if top in _active_ad_ids(policy, now):
        result["map_pin"] = "active_ad_on_top"
        return result
    result["map_pin"] = "would_send"
    if not apply:
        result["owner_action_required"] = True
        return result
    message_id = await bot.send_map_pin_message(CHANNEL, MAP_PIN_TEXT, MAP_PIN_MARKUP)
    result.update(map_pin="sent", map_channel_message=message_id)
    if await bot._call("pinChatMessage", {"chat_id": CHANNEL, "message_id": message_id,
                                          "disable_notification": True}) is not True:
        raise TelegramAPIError("pinChatMessage not confirmed")
    result["map_pin"] = "pinned"
    if type(current) is int and current != message_id:
        try:
            unpinned = await bot._call("unpinChatMessage",
                                       {"chat_id": CHANNEL, "message_id": current})
            result["previous_unpinned"] = unpinned is True
        except (httpx.HTTPError, TelegramAPIError):
            result["previous_unpinned"] = False
    # Новый ID ещё не записан в pin-policy.json: без этого цикл не узнает пост.
    result["owner_action_required"] = True
    return result
