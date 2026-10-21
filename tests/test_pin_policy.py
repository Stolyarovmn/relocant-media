from datetime import UTC, datetime

import pytest

from relocant_media.services.pin_policy import CHANNEL, DISCUSSION, maintain_pins

NOW = datetime(2026, 10, 21, 6, tzinfo=UTC)


def policy(ads=None):
    return dict(channel_id=CHANNEL, discussion_id=DISCUSSION,
                service_channel_messages=[4, 6], service_discussion_messages=[2, 3],
                advertisements=ads or [])


def forward(mid, original):
    return dict(message_id=mid, text="[тестовый запуск] Новость", is_automatic_forward=True,
                forward_origin=dict(type="channel", chat=dict(id=CHANNEL), message_id=original))


class Bot:
    def __init__(self, channel=None, discussion=None, denied=False):
        self.pins = {CHANNEL: channel or [dict(message_id=6)], DISCUSSION: discussion or []}
        self.calls = []
        self.denied = denied

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        if self.denied and payload["chat_id"] == DISCUSSION:
            raise RuntimeError("Доступ не предоставлен")
        pins = self.pins[payload["chat_id"]]
        if method == "getChat":
            return {"pinned_message": pins[0]} if pins else {}
        assert method == "unpinChatMessage"
        assert pins[0]["message_id"] == payload["message_id"]
        pins.pop(0)
        return True


@pytest.mark.asyncio
async def test_news_auto_pins_removed_without_touching_services():
    bot = Bot(discussion=[forward(12, 15), forward(11, 14), dict(message_id=3)])
    result = await maintain_pins(bot, policy(), now=NOW)
    assert [r["message_id"] for r in result["unpinned"]] == [12, 11]
    assert bot.pins[DISCUSSION] == [dict(message_id=3)]
    assert not result["owner_action_required"]


@pytest.mark.asyncio
async def test_unknown_pin_and_missing_rights_do_not_expand_access_or_send():
    bot = Bot(discussion=[dict(message_id=30, text="Чужой закреп")])
    result = await maintain_pins(bot, policy(), now=NOW)
    assert result["blocked"][0]["reason"] == "unknown_pin"
    denied = Bot(denied=True)
    result = await maintain_pins(denied, policy(), now=NOW)
    assert result["owner_action_required"] and not result["unpinned"]
    assert all(m == "getChat" for m, _ in denied.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("paid,approved,expected", [(True, True, 0), (False, True, 1),
                                                   (True, False, 1)])
async def test_paid_ad_within_term_is_preserved(paid, approved, expected):
    ad = dict(channel_message_id=20, paid=paid, owner_approved=approved,
              starts_at="2026-10-21T05:00:00+00:00", ends_at="2026-10-21T07:00:00+00:00")
    bot = Bot(discussion=[forward(17, 20), dict(message_id=3)])
    result = await maintain_pins(bot, policy([ad]), now=NOW)
    assert len(result["unpinned"]) == expected


@pytest.mark.asyncio
async def test_ad_expires_at_end_boundary():
    ad = dict(channel_message_id=20, paid=True, owner_approved=True,
              starts_at="2026-10-21T05:00:00+00:00", ends_at=NOW.isoformat())
    bot = Bot(channel=[dict(message_id=20, text="Реклама"), dict(message_id=6)],
              discussion=[forward(17, 20), dict(message_id=3)])
    result = await maintain_pins(bot, policy([ad]), now=NOW)
    assert len(result["unpinned"]) == 2
