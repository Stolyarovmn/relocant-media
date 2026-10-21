"""Remove news auto-pins, preserve service posts and explicitly paid timed ads."""
from datetime import UTC, datetime
from typing import Any

from relocant_media.bot.api import TelegramBotAPI

CHANNEL = -1003967014133
DISCUSSION = -1003942640600


def _ad_active(ad: dict[str, Any], now: datetime) -> bool:
    if ad.get("paid") is not True or ad.get("owner_approved") is not True:
        return False
    try:
        start = datetime.fromisoformat(ad["starts_at"])
        end = datetime.fromisoformat(ad["ends_at"])
        message_id = ad.get("channel_message_id")
        return bool(type(message_id) is int and message_id > 0
                    and start.tzinfo and end.tzinfo and start <= now < end)
    except (KeyError, TypeError, ValueError):
        return False


async def maintain_pins(bot: TelegramBotAPI, policy: dict[str, Any], *,
                        now: datetime | None = None) -> dict[str, Any]:
    """No pin/send/delete capability here. Does not consume webhook or poll updates."""
    if policy.get("channel_id") != CHANNEL or policy.get("discussion_id") != DISCUSSION:
        raise ValueError("pin policy targets are not approved")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("pin policy requires an aware time")
    ads = policy.get("advertisements", [])
    active = {ad.get("channel_message_id") for ad in ads if _ad_active(ad, now)}
    registered_ads = {ad["channel_message_id"] for ad in ads
                      if ad.get("paid") is True and ad.get("owner_approved") is True
                      and type(ad.get("channel_message_id")) is int
                      and ad["channel_message_id"] > 0}
    result: dict[str, Any] = {"unpinned": [], "blocked": [], "preserved": []}
    for chat, field in [(CHANNEL, "service_channel_messages"),
                        (DISCUSSION, "service_discussion_messages")]:
        service = set(policy[field])
        for _ in range(12):
            try:
                info = await bot._call("getChat", {"chat_id": chat})
                if not isinstance(info, dict):
                    raise ValueError("invalid getChat response")
                pin = info.get("pinned_message")
                if not isinstance(pin, dict):
                    break
                message_id = pin.get("message_id")
                if type(message_id) is not int or message_id <= 0:
                    raise ValueError("invalid pinned message id")
                origin = pin.get("forward_origin", {})
                linked = (chat == DISCUSSION and pin.get("is_automatic_forward") is True
                          and origin.get("type") == "channel"
                          and origin.get("chat", {}).get("id") == CHANNEL)
                original_id = message_id if chat == CHANNEL else origin.get("message_id")
                if message_id in service or original_id in active:
                    result["preserved"].append({"chat_id": chat, "message_id": message_id})
                    break
                known_news = ((chat == CHANNEL or linked)
                              and str(pin.get("text", pin.get("caption", ""))).startswith(
                                  "[тестовый запуск]"))
                if not known_news and original_id not in registered_ads:
                    result["blocked"].append({"chat_id": chat, "reason": "unknown_pin"})
                    break
                unpinned = await bot._call("unpinChatMessage", {
                    "chat_id": chat, "message_id": message_id})
                if unpinned is not True:
                    raise ValueError("unpin not confirmed")
                result["unpinned"].append({"chat_id": chat, "message_id": message_id})
            except Exception as error:
                result["blocked"].append({"chat_id": chat, "reason": "pin_access_or_api_error",
                                          "error_type": type(error).__name__})
                break
        else:
            result["blocked"].append({"chat_id": chat, "reason": "pin_cleanup_limit"})
    result["owner_action_required"] = bool(result["blocked"])
    return result
