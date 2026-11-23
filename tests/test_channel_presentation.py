import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from relocant_media.bot.api import TelegramBotAPI, inline_keyboards_match
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.domain.topic_tags import TAGS_V2_PRESENTATION, topic_tags
from relocant_media.services.channel_presentation import ensure_channel_description

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
    assert MAP_HOME_URL in draft["description"].split()
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
async def test_channel_description_preview_and_repeat_do_not_edit_pins():
    presentation = yaml.safe_load(PRESENTATION.read_text(encoding="utf-8"))
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    channel = {"id": policy["channel_id"], "type": "channel"}
    info = {**channel, "description": "Старое описание", "pinned_message": {"message_id": 6}}
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
        else:
            assert method == "setChatDescription"
            assert data == {"chat_id": channel["id"], "description": presentation["description"]}
            info["description"] = data["description"]
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        preview = await ensure_channel_description(bot, policy, presentation, apply=False)
        assert preview == {
            "description": "would_update", "description_permission": "granted",
            "owner_action_required": False,
        }
        assert calls == ["getChat", "getMe", "getChatMember"]
        applied = await ensure_channel_description(bot, policy, presentation, apply=True)
        assert applied["description"] == "updated"
        repeat = await ensure_channel_description(bot, policy, presentation, apply=True)
        assert repeat["description"] == "unchanged"
    assert calls == [
        "getChat", "getMe", "getChatMember",
        "getChat", "getMe", "getChatMember", "setChatDescription", "getChat",
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


CHANNEL_ID = -1003967014133


class MapPinBot:
    def __init__(self, top, *, markup=None, pin_ok=True, send_error=None):
        self.top, self.markup, self.pin_ok, self.send_error = top, markup, pin_ok, send_error
        self.calls = []

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        if method == "getChat":
            info = {"id": CHANNEL_ID, "type": "channel"}
            if self.top is not None:
                info["pinned_message"] = {"message_id": self.top,
                                          **({"reply_markup": self.markup} if self.markup else {})}
            return info
        if method == "pinChatMessage":
            assert payload["disable_notification"] is True
            if self.pin_ok is not True:
                raise RuntimeError("pin failed")
            self.top = payload["message_id"]
            return True
        if method == "editMessageReplyMarkup":
            return {"message_id": payload["message_id"], "chat": {"id": CHANNEL_ID}}
        assert method == "unpinChatMessage"
        return True

    async def edit_inline_keyboard(self, chat_id, message_id, markup):
        self.calls.append(("editMessageReplyMarkup", {"reply_markup": markup}))
        return True

    async def send_map_pin_message(self, chat_id, text, markup):
        self.calls.append(("sendMessage", {"text": text, "reply_markup": markup}))
        if self.send_error:
            raise self.send_error
        return 200


def _policy(**extra):
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    return {**policy, **extra}


def _ad(message_id):
    return dict(channel_message_id=message_id, paid=True, owner_approved=True,
                starts_at="2026-11-23T00:00:00+00:00", ends_at="2026-11-24T00:00:00+00:00")


AD_TIME = __import__("datetime").datetime(2026, 11, 23, 12, tzinfo=__import__("datetime").UTC)


@pytest.mark.asyncio
async def test_map_pin_is_checked_without_sending_by_default():
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    bot = MapPinBot(top=6)
    result = await ensure_map_pin_on_top(bot, _policy(), apply=False)
    assert result["map_pin"] == "would_send" and result["owner_action_required"]
    assert [m for m, _ in bot.calls] == ["getChat"]
    bot = MapPinBot(top=150)
    result = await ensure_map_pin_on_top(bot, _policy(map_channel_message=150), apply=True)
    assert result["map_pin"] == "on_top" and [m for m, _ in bot.calls] == ["getChat"]


@pytest.mark.asyncio
@pytest.mark.parametrize("top,expected,action", [
    (170, "active_ad_on_top", False), (6, "active_ad_not_on_top", True),
    (150, "active_ad_not_on_top", True)])
async def test_active_paid_ad_blocks_the_map_post(top, expected, action):
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    bot = MapPinBot(top=top)
    result = await ensure_map_pin_on_top(
        bot, _policy(map_channel_message=150, advertisements=[_ad(170)]), apply=True,
        now=AD_TIME)
    assert result["map_pin"] == expected and result["owner_action_required"] is action
    assert [m for m, _ in bot.calls] == ["getChat"]


@pytest.mark.asyncio
async def test_apply_moves_the_map_button_to_one_silent_map_post():
    from relocant_media.services.channel_presentation import (
        MAP_PIN_MARKUP,
        MAP_PIN_TEXT,
        ensure_map_pin_on_top,
    )

    other = {"text": "Написать", "url": "https://t.me/relocant_media_belgrade_bot"}
    markup = {"inline_keyboard": [[{"text": "🗺 Открыть карту", "url": MAP_HOME_URL}], [other]]}
    bot = MapPinBot(top=6, markup=markup)
    result = await ensure_map_pin_on_top(bot, _policy(map_channel_message=150), apply=True)
    assert MAP_PIN_TEXT.splitlines()[0] == "🗺 Посмотреть на карте"
    assert MAP_PIN_MARKUP["inline_keyboard"][0][0] == {
        "text": "Посмотреть на карте", "url": MAP_HOME_URL, "style": "success"}
    assert [m for m, _ in bot.calls] == [
        "getChat", "sendMessage", "pinChatMessage", "editMessageReplyMarkup",
        "unpinChatMessage"]
    assert bot.calls[3][1]["reply_markup"] == {"inline_keyboard": [[other]]}
    assert result["service_button"] == "removed"
    assert result["map_channel_message"] == 200 and result["map_pin"] == "pinned"
    assert bot.calls[-1][1] == {"chat_id": CHANNEL_ID, "message_id": 150}


@pytest.mark.asyncio
async def test_only_map_button_on_service_post_is_cleared_to_an_empty_keyboard():
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    bot = MapPinBot(top=6, markup={"inline_keyboard": [[{"text": "x", "url": MAP_HOME_URL}]]})
    await ensure_map_pin_on_top(bot, _policy(), apply=True)
    assert bot.calls[3] == ("editMessageReplyMarkup", {
        "chat_id": CHANNEL_ID, "message_id": 6, "reply_markup": {"inline_keyboard": []}})


@pytest.mark.asyncio
async def test_failed_pin_keeps_the_sent_id_and_existing_id_is_pinned_without_resending():
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    bot = MapPinBot(top=6, pin_ok=False)
    result = await ensure_map_pin_on_top(bot, _policy(), apply=True)
    assert result["map_pin"] == "sent_not_pinned" and result["map_channel_message"] == 200
    retry = MapPinBot(top=6)
    result = await ensure_map_pin_on_top(retry, _policy(), apply=True, pin_existing=200)
    assert result["map_pin"] == "pinned"
    assert "sendMessage" not in [m for m, _ in retry.calls]


@pytest.mark.asyncio
async def test_uncertain_send_is_not_retried_or_pinned():
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    bot = MapPinBot(top=6, send_error=httpx.ReadTimeout("timeout"))
    result = await ensure_map_pin_on_top(bot, _policy(), apply=True)
    assert result["map_pin"] == "send_uncertain" and result["owner_action_required"]
    assert [m for m, _ in bot.calls] == ["getChat", "sendMessage"]


@pytest.mark.asyncio
async def test_map_pin_message_is_silent_without_link_preview():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 201}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        assert await bot.send_map_pin_message(-1003967014133, "🗺 Посмотреть на карте",
                                              {"inline_keyboard": []}) == 201
    assert sent[0]["disable_notification"] is True
    assert sent[0]["link_preview_options"] == {"is_disabled": True}


@pytest.mark.asyncio
async def test_unregistered_map_post_on_top_is_recognised_and_not_resent():
    from relocant_media.services.channel_presentation import (
        MAP_PIN_MARKUP,
        MAP_PIN_TEXT,
        ensure_map_pin_on_top,
    )

    bot = MapPinBot(top=200, markup={"inline_keyboard": [[
        {k: v for k, v in MAP_PIN_MARKUP["inline_keyboard"][0][0].items() if k != "style"}]]})
    original = bot._call

    async def with_text(method, payload):
        info = await original(method, payload)
        if method == "getChat":
            info["pinned_message"]["text"] = MAP_PIN_TEXT
        return info

    bot._call = with_text
    result = await ensure_map_pin_on_top(bot, _policy(), apply=True)
    assert result["map_pin"] == "on_top_unregistered" and result["map_channel_message"] == 200
    assert [m for m, _ in bot.calls] == ["getChat"]


@pytest.mark.asyncio
async def test_service_button_is_kept_when_the_new_pin_fails():
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    bot = MapPinBot(top=6, markup={"inline_keyboard": [[{"text": "x", "url": MAP_HOME_URL}]]},
                    pin_ok=False)
    result = await ensure_map_pin_on_top(bot, _policy(), apply=True)
    assert result["map_pin"] == "sent_not_pinned" and result["map_channel_message"] == 200
    assert "editMessageReplyMarkup" not in [m for m, _ in bot.calls]
    assert result["service_button"] == "would_remove"


@pytest.mark.asyncio
async def test_failed_button_cleanup_is_reported_with_the_pinned_id():
    from relocant_media.services.channel_presentation import ensure_map_pin_on_top

    other = {"text": "Написать", "url": "https://t.me/relocant_media_belgrade_bot"}
    bot = MapPinBot(top=6, markup={"inline_keyboard": [[{"text": "x", "url": MAP_HOME_URL},
                                                         other]]})

    async def fail(chat_id, message_id, markup):
        raise RuntimeError("edit failed")

    bot.edit_inline_keyboard = fail
    result = await ensure_map_pin_on_top(bot, _policy(), apply=True)
    assert result["map_pin"] == "pinned" and result["map_channel_message"] == 200
    assert result["service_button"] == "remove_failed" and result["owner_action_required"]
