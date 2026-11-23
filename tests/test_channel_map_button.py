from pathlib import Path

import httpx
import pytest
import yaml

from relocant_media.bot.api import (
    TelegramAPIError,
    map_home_reply_markup,
    map_reply_markup,
    same_reply_markup,
)
from relocant_media.domain.map_links import MAP_HOME_URL
from relocant_media.ops.cycle_audit import audit_cycle
from relocant_media.services.channel_presentation import (
    apply_presentation,
    ensure_service_pin_map_button,
    has_home_map_button,
    load_presentation,
)
from relocant_media.services.pin_policy import CHANNEL, DISCUSSION

BELGRADE = Path(__file__).resolve().parents[1] / "cities" / "belgrade"
HOME = map_home_reply_markup()
EVENT_URL = "https://t.me/relocant_media_belgrade_bot?startapp=event_0123456789abcdef0123"


def policy():
    return dict(channel_id=CHANNEL, discussion_id=DISCUSSION,
                service_channel_messages=[4, 6], service_discussion_messages=[2, 3],
                advertisements=[])


class Bot:
    def __init__(self, pin=None, description="старое", fail=None, echo_style=True):
        self.pin, self.description, self.fail = pin, description, fail
        self.echo_style = echo_style
        self.calls = []

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        if method == "getChat":
            info = {"id": CHANNEL, "description": self.description}
            if self.pin is not None:
                info["pinned_message"] = self.pin
            return info
        if self.fail and method in self.fail:
            raise self.fail[method]
        if method == "setChatDescription":
            self.description = payload["description"]
            return True
        assert method in {"editMessageReplyMarkup", "editMessageText"}
        assert payload["chat_id"] == CHANNEL
        markup = payload["reply_markup"]
        if not self.echo_style:
            markup = {"inline_keyboard": [[{k: v for k, v in b.items() if k != "style"}
                                           for b in row] for row in markup["inline_keyboard"]]}
        return {"message_id": payload["message_id"], "chat": {"id": CHANNEL},
                "reply_markup": markup}


def not_modified():
    request = httpx.Request("POST", "https://api.telegram.org/bot/x")
    response = httpx.Response(400, request=request, json={
        "ok": False, "description": "Bad Request: message is not modified: same"})
    return httpx.HTTPStatusError("400", request=request, response=response)


def test_every_map_button_is_green():
    assert map_reply_markup(EVENT_URL)["inline_keyboard"][0][0]["style"] == "success"
    button = HOME["inline_keyboard"][0][0]
    assert button == {"text": "🗺 Открыть карту", "url": MAP_HOME_URL, "style": "success"}
    with pytest.raises(ValueError):
        map_reply_markup(MAP_HOME_URL)


def test_receipt_check_accepts_clients_that_do_not_echo_style():
    plain = {"inline_keyboard": [[{"text": "🗺 Открыть карту", "url": MAP_HOME_URL}]]}
    assert same_reply_markup(plain, HOME)
    other = {"inline_keyboard": [[{"text": "🗺 Открыть карту", "url": MAP_HOME_URL,
                                   "style": "danger"}]]}
    assert not same_reply_markup(other, HOME)
    assert not same_reply_markup(None, HOME)


def test_channel_description_links_the_same_map_as_the_pin_button():
    presentation = load_presentation(BELGRADE / "channel-presentation.yaml")
    lines = presentation["description"].splitlines()
    # Карта видна в превью канала: не дальше второй строки.
    assert any(MAP_HOME_URL.removeprefix("https://") in line for line in lines[:2])
    assert presentation["navigation"]["map_button"] == HOME["inline_keyboard"][0][0]
    raw = yaml.safe_load((BELGRADE / "channel-presentation.yaml").read_text(encoding="utf-8"))
    raw["description"] = "Без карты"
    path = Path(__file__).parent / "_presentation_tmp.yaml"
    try:
        path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        with pytest.raises(ValueError):
            load_presentation(path)
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_top_service_pin_gets_map_button_only_keyboard_is_edited():
    bot = Bot(pin={"message_id": 6, "text": "Сотрудничество"})
    result = await ensure_service_pin_map_button(bot, policy())
    assert result["status"] == "edited" and not result["owner_action_required"]
    method, payload = bot.calls[-1]
    assert method == "editMessageReplyMarkup"
    assert payload == {"chat_id": CHANNEL, "message_id": 6, "reply_markup": HOME}


@pytest.mark.asyncio
async def test_present_button_news_and_ads_are_left_alone():
    bot = Bot(pin={"message_id": 4, "reply_markup": HOME})
    assert (await ensure_service_pin_map_button(bot, policy()))["status"] == "present"
    bot = Bot(pin={"message_id": 77, "text": "Реклама"})
    assert (await ensure_service_pin_map_button(bot, policy()))["status"] == "top_pin_not_service"
    bot = Bot()
    assert (await ensure_service_pin_map_button(bot, policy()))["status"] == "top_pin_not_service"
    assert all(m == "getChat" for b in [bot] for m, _ in b.calls)


@pytest.mark.asyncio
async def test_edit_denied_is_reported_without_retries_or_new_posts():
    bot = Bot(pin={"message_id": 6},
              fail={"editMessageReplyMarkup": TelegramAPIError("message can't be edited")})
    result = await ensure_service_pin_map_button(bot, policy())
    assert result["status"] == "blocked" and result["owner_action_required"]
    assert [m for m, _ in bot.calls] == ["getChat", "editMessageReplyMarkup"]


@pytest.mark.asyncio
async def test_wrong_policy_target_is_rejected():
    bad = {**policy(), "channel_id": -100}
    with pytest.raises(ValueError):
        await ensure_service_pin_map_button(Bot(), bad)


def test_has_home_map_button():
    assert has_home_map_button(HOME)
    assert not has_home_map_button(map_reply_markup(EVENT_URL))
    assert not has_home_map_button({"inline_keyboard": [[{"text": "x", "url": MAP_HOME_URL,
                                                          "style": "danger"}]]})


@pytest.mark.asyncio
async def test_apply_presentation_is_idempotent_and_never_sends_or_pins():
    presentation = load_presentation(BELGRADE / "channel-presentation.yaml")
    bot = Bot(echo_style=False)
    result = await apply_presentation(bot, policy(), presentation)
    assert result == {"description": "edited", "navigation": "edited",
                      "service_buttons": {"6": "edited"}}
    text = next(p for m, p in bot.calls if m == "editMessageText")
    assert text["message_id"] == 4 and text["parse_mode"] == "HTML"
    assert text["text"] == presentation["navigation"]["html"]
    assert text["reply_markup"] == HOME
    again = Bot(description=presentation["description"], fail={
        "editMessageText": not_modified(), "editMessageReplyMarkup": not_modified()})
    result = await apply_presentation(again, policy(), presentation)
    assert result == {"description": "unchanged", "navigation": "unchanged",
                      "service_buttons": {"6": "unchanged"}}
    for b in (bot, again):
        assert {m for m, _ in b.calls} <= {"getChat", "setChatDescription",
                                           "editMessageText", "editMessageReplyMarkup"}


def test_cycle_audit_flags_blocked_map_button(tmp_path):
    report = dict(status="PRIMARY_OK", fallback_required=False, channel_transport="bot-api",
                  pin_maintenance=dict(blocked=[], owner_action_required=False,
                                       map_button=dict(status="blocked")))
    assert audit_cycle(None, report)["checks"]["service_pin_map_button"] is False
    report["pin_maintenance"]["map_button"] = dict(status="present")
    assert audit_cycle(None, report)["checks"]["service_pin_map_button"] is True
