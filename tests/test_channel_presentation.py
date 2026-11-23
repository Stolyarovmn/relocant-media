import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from relocant_media.bot.api import TelegramBotAPI, inline_keyboards_match
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.domain.topic_tags import TAGS_V2_PRESENTATION, topic_tags
from relocant_media.services.channel_presentation import (
    ensure_channel_description,
    ensure_service_pin_map_button,
)

BELGRADE = Path(__file__).resolve().parents[1] / "cities" / "belgrade"
PRESENTATION = BELGRADE / "channel-presentation.yaml"


def test_belgrade_navigation_uses_current_tags_and_public_links():
    draft = yaml.safe_load((BELGRADE / "channel-presentation.yaml").read_text(encoding="utf-8"))
    pin_policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    navigation = draft["navigation"]
    assert 0 < len(draft["description"]) <= 255
    assert navigation["message_id"] in pin_policy["service_channel_messages"]

    text = navigation["html"]
    expected = {
        topic_tags(category, presentation_version=TAGS_V2_PRESENTATION)
        for category in ("transport", "utilities", "events", "culture", "sport", "city")
    }
    expected.add(topic_tags(
        "city", presentation_version=TAGS_V2_PRESENTATION, rubric_id="city_life",
    ))
    assert set(re.findall(r"#[\w]+", text)) == expected

    bot = "https://t.me/relocant_media_belgrade_bot"
    ads = f"https://t.me/{draft['channel']}/{pin_policy['service_channel_messages'][1]}"
    assert set(re.findall(r'href="([^"]+)"', text)) == {bot, ads}
    assert navigation["map_button"]["url"] == f"{bot}?startapp"
    assert MAP_HOME_URL in draft["description"].split()
    assert navigation["map_button"]["style"] == "success"
    assert "t.me/c/" not in draft["description"] + text


def test_belgrade_navigation_lists_the_configured_digest_times():
    draft = yaml.safe_load((BELGRADE / "channel-presentation.yaml").read_text(encoding="utf-8"))
    schedule = yaml.safe_load((BELGRADE / "digests.yaml").read_text(encoding="utf-8"))
    text = draft["navigation"]["html"]
    assert all(schedule[slot] in text for slot in ("morning", "evening"))


def test_keyboard_receipt_accepts_only_the_absence_of_style():
    home = {"text": "🗺 Открыть карту", "url": MAP_HOME_URL}
    callback = {"text": "Меню", "callback_data": "menu"}
    expected = {"inline_keyboard": [[{**home, "style": "success"}, callback]]}
    assert inline_keyboards_match({"inline_keyboard": [[home, callback]]}, expected)
    assert not inline_keyboards_match({
        "inline_keyboard": [[{**home, "style": "primary"}, callback]],
    }, expected)
    assert not inline_keyboards_match({
        "inline_keyboard": [[home, {**callback, "callback_data": "other"}]],
    }, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [4, 6])
@pytest.mark.parametrize("echo_style", [True, False])
async def test_service_pin_update_preserves_other_buttons_and_checks_before_writing(
    message_id, echo_style,
):
    presentation = yaml.safe_load(PRESENTATION.read_text(encoding="utf-8"))
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    button = presentation["navigation"]["map_button"]
    other_button = {"text": "Партнёрство", "url": "https://example.net", "style": "primary"}
    channel = {"id": policy["channel_id"], "type": "channel"}
    old_button = {**button, "style": "primary"} if echo_style else {
        "text": "Карта", "url": MAP_HOME_URL,
    }
    old_markup = {"inline_keyboard": [[old_button, other_button]]}
    pin = {"message_id": message_id, "reply_markup": old_markup, "chat": channel}
    info = {**channel, "description": "Старое описание", "pinned_message": pin}
    calls = []

    def handler(request):
        method = request.url.path.split("/")[-1]
        calls.append(method)
        data = json.loads(request.content)
        if method == "getMe":
            assert data == {}
            result = {"id": 789}
        elif method == "getChatMember":
            assert data == {"chat_id": channel["id"], "user_id": 789}
            result = {"status": "administrator", "can_change_info": True}
        elif method == "getChat":
            assert data["chat_id"] == channel["id"]
            result = info
        elif method == "setChatDescription":
            assert data["chat_id"] == channel["id"]
            info["description"] = data["description"]
            result = True
        else:
            assert method == "editMessageReplyMarkup"
            assert data == {
                "chat_id": channel["id"], "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[button], [other_button]]},
            }
            rows = data["reply_markup"]["inline_keyboard"]
            if not echo_style:
                rows = [[{k: v for k, v in item.items() if k != "style"} for item in row]
                        for row in rows]
            pin["reply_markup"] = {"inline_keyboard": rows}
            result = pin
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        description_preview = await ensure_channel_description(
            bot, policy, presentation, apply=False,
        )
        assert description_preview == {
            "description": "would_update", "description_permission": "granted",
            "owner_action_required": False,
        }
        assert calls == ["getChat", "getMe", "getChatMember"]
        preview = await ensure_service_pin_map_button(
            bot, policy, presentation, apply=False,
        )
        assert calls == ["getChat", "getMe", "getChatMember", "getChat"]
        assert preview == {
            "map_button": "would_update",
            "message_id": message_id, "owner_action_required": False,
        }
        assert (await ensure_channel_description(
            bot, policy, presentation, apply=True,
        ))["description"] == "updated"
        assert (await ensure_service_pin_map_button(
            bot, policy, presentation, apply=True,
        ))["map_button"] == "updated"
        assert (await ensure_channel_description(
            bot, policy, presentation, apply=True,
        ))["description"] == "unchanged"
        assert (await ensure_service_pin_map_button(
            bot, policy, presentation, apply=True,
        ))["map_button"] == "unchanged"
    assert calls == [
        "getChat", "getMe", "getChatMember", "getChat",
        "getChat", "getMe", "getChatMember", "setChatDescription",
        "getChat", "editMessageReplyMarkup", "getChat", "getChat",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [False, True])
async def test_description_permission_denial_only_reads_telegram(apply):
    presentation = yaml.safe_load(PRESENTATION.read_text(encoding="utf-8"))
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    channel = {"id": policy["channel_id"], "type": "channel"}
    calls = []

    def handler(request):
        method = request.url.path.split("/")[-1]
        calls.append(method)
        if method == "getChat":
            result = {**channel, "pinned_message": {"message_id": 6}}
        elif method == "getMe":
            result = {"id": 789}
        else:
            assert method == "getChatMember"
            result = {"status": "administrator", "can_change_info": False}
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ensure_channel_description(
            TelegramBotAPI("test-token", client), policy, presentation,
            apply=apply,
        )
    assert result["description_permission"] == "denied"
    assert result["owner_action_required"]
    assert calls == ["getChat", "getMe", "getChatMember"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "allowlist_ad", "http_status", "description", "expected", "expected_calls"),
    [
        (149, False, 403, "Forbidden", "no_service_pin", ["getChat"]),
        (6, True, 403, "Forbidden", "no_service_pin", ["getChat"]),
        (4, False, 403, "Forbidden",
         "owner_action_required", ["getChat", "editMessageReplyMarkup"]),
        (6, False, 400, "Bad Request: message is not modified",
         "unchanged", ["getChat", "editMessageReplyMarkup"]),
    ],
)
async def test_non_service_pins_and_failed_edit_never_create_messages(
    message_id, allowlist_ad, http_status, description, expected, expected_calls,
):
    presentation = yaml.safe_load(PRESENTATION.read_text(encoding="utf-8"))
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    if allowlist_ad:
        policy["advertisements"] = [{"channel_message_id": message_id}]
    calls = []

    def handler(request):
        method = request.url.path.split("/")[-1]
        calls.append(method)
        if method == "getChat":
            return httpx.Response(200, json={"ok": True, "result": {
                "id": policy["channel_id"], "type": "channel",
                "pinned_message": {"message_id": message_id},
            }})
        assert method == "editMessageReplyMarkup"
        return httpx.Response(http_status, json={"ok": False, "description": description})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await ensure_service_pin_map_button(
            TelegramBotAPI("test-token", client), policy, presentation, apply=True,
        )
    assert result["map_button"] == expected
    assert result["owner_action_required"] is (expected == "owner_action_required")
    assert calls == expected_calls
