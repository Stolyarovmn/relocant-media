import json
from datetime import UTC, datetime

import httpx
import pytest

from relocant_media.bot.api import PhotoUpload, TelegramBotAPI
from relocant_media.domain.channel_targets import APPROVED_CHANNEL_ID
from relocant_media.domain.quiet_hours import is_quiet_time, silent_channel_post

NIGHT = datetime(2026, 11, 21, 23, 30, tzinfo=UTC)  # 00:30 в Белграде
DAY = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


@pytest.mark.parametrize(("moment", "quiet"), [
    (datetime(2026, 11, 21, 21, 59, tzinfo=UTC), False),  # 22:59 зимой
    (datetime(2026, 11, 21, 22, 0, tzinfo=UTC), True),    # 23:00
    (datetime(2026, 11, 22, 6, 59, tzinfo=UTC), True),    # 07:59
    (datetime(2026, 11, 22, 7, 0, tzinfo=UTC), False),    # 08:00
    (datetime(2026, 7, 1, 20, 59, tzinfo=UTC), False),    # 22:59 летом
    (datetime(2026, 7, 1, 21, 0, tzinfo=UTC), True),      # 23:00 летом
    (datetime(2026, 7, 2, 6, 0, tzinfo=UTC), False),      # 08:00 летом
])
def test_quiet_window_follows_belgrade_local_time(moment: datetime, quiet: bool) -> None:
    assert is_quiet_time(moment) is quiet


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        is_quiet_time(datetime(2026, 11, 21, 23, 30))


def test_only_the_production_channel_is_silenced() -> None:
    assert silent_channel_post(APPROVED_CHANNEL_ID, lambda: NIGHT)
    assert silent_channel_post(str(APPROVED_CHANNEL_ID), lambda: NIGHT)
    assert not silent_channel_post(APPROVED_CHANNEL_ID, lambda: DAY)
    assert not silent_channel_post(123456, lambda: NIGHT)
    assert not silent_channel_post("@someone", lambda: NIGHT)


def _bot(seen: list[httpx.Request], clock: datetime) -> TelegramBotAPI:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("sendMediaGroup"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"message_id": 7}, {"message_id": 8}]})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramBotAPI("token", client, clock=lambda: clock)


def _flag(request: httpx.Request) -> object:
    if request.headers["content-type"].startswith("application/json"):
        return json.loads(request.content).get("disable_notification")
    body = request.content.decode("latin-1")
    return "true" if 'name="disable_notification"\r\n\r\ntrue' in body else None


async def _send_all(bot: TelegramBotAPI, chat_id: int) -> None:
    photo = PhotoUpload("a.png", PNG)
    await bot.send_message(chat_id, "Текст")
    await bot.send_notice_message(chat_id, "<b>Текст</b>", source_url="https://example.org/a")
    await bot.send_rich_message(chat_id, "<p>Текст</p>")
    await bot.send_rich_message(chat_id, '<img src="tg://photo?id=notice_cover">',
                                photo=photo)
    await bot.send_photo(chat_id, photo, "Подпись")
    await bot.send_media_group(chat_id, [photo, photo], "Подпись")


@pytest.mark.asyncio
async def test_night_channel_posts_are_silent_in_every_send_method() -> None:
    seen: list[httpx.Request] = []
    await _send_all(_bot(seen, NIGHT), APPROVED_CHANNEL_ID)
    assert len(seen) == 6
    assert [_flag(request) for request in seen] == [True, True, True, "true", "true", "true"]


@pytest.mark.asyncio
async def test_daytime_and_private_chats_keep_normal_notifications() -> None:
    seen: list[httpx.Request] = []
    await _send_all(_bot(seen, DAY), APPROVED_CHANNEL_ID)
    await _send_all(_bot(seen, NIGHT), 424242)
    assert len(seen) == 12
    assert all(_flag(request) is None for request in seen)
