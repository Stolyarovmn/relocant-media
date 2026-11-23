import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.domain.topic_tags import TAGS_V2_PRESENTATION, topic_tags
from relocant_media.services.channel_presentation import ensure_service_pin_map_button

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


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [4, 6])
async def test_service_pin_update_preserves_other_buttons_and_checks_before_writing(message_id):
    presentation = yaml.safe_load(PRESENTATION.read_text(encoding="utf-8"))
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    button = presentation["navigation"]["map_button"]
    other_button = {"text": "Партнёрство", "url": "https://example.net", "style": "primary"}
    channel = {"id": policy["channel_id"], "type": "channel"}
    old_markup = {"inline_keyboard": [[{"text": "Карта", "url": MAP_HOME_URL}, other_button]]}
    pin = {"message_id": message_id, "reply_markup": old_markup, "chat": channel}
    info = {**channel, "description": "Старое описание", "pinned_message": pin}
    calls = []

    def handler(request):
        method = request.url.path.split("/")[-1]
        calls.append(method)
        data = json.loads(request.content)
        assert data["chat_id"] == channel["id"]
        if method == "getChat":
            result = info
        elif method == "setChatDescription":
            info["description"] = data["description"]
            result = True
        else:
            assert method == "editMessageReplyMarkup"
            assert data == {
                "chat_id": channel["id"], "message_id": message_id,
                "reply_markup": {"inline_keyboard": [[button], [other_button]]},
            }
            pin["reply_markup"] = data["reply_markup"]
            result = pin
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        preview = await ensure_service_pin_map_button(
            bot, policy, presentation, apply=False, update_description=True,
        )
        assert calls == ["getChat"]
        assert preview == {
            "description": "would_update", "map_button": "would_update",
            "message_id": message_id, "owner_action_required": False,
        }
        applied = await ensure_service_pin_map_button(
            bot, policy, presentation, apply=True, update_description=True,
        )
        assert applied["description"] == applied["map_button"] == "updated"
        repeat = await ensure_service_pin_map_button(
            bot, policy, presentation, apply=True, update_description=True,
        )
        assert repeat["description"] == repeat["map_button"] == "unchanged"
    assert calls == [
        "getChat", "getChat", "setChatDescription", "editMessageReplyMarkup", "getChat",
    ]


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
