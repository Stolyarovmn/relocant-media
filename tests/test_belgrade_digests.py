import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from relocant_media.collectors.rhmz_forecast import parse_forecast, parse_warning
from relocant_media.domain.map_links import is_map_event_url
from relocant_media.ops.belgrade_digests import Entry, deliver, due_slot, render

_ZONE = ZoneInfo("Europe/Belgrade")


def test_two_week_slots_use_belgrade_time():
    first = due_slot(datetime(2026, 11, 13, 19, 30, tzinfo=UTC))
    assert first is not None and first.kind == "evening"
    assert due_slot(datetime(2026, 11, 13, 19, 29, tzinfo=UTC)) is None
    morning = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert morning is not None and morning.kind == "morning"
    assert due_slot(datetime(2026, 11, 14, 7, 15, tzinfo=UTC)) is None
    assert due_slot(datetime(2026, 11, 27, 6, 30, tzinfo=UTC)) is not None
    assert due_slot(datetime(2026, 11, 27, 19, 30, tzinfo=UTC)) is None


def test_forecast_uses_dated_belgrade_column_and_skips_stale_html():
    html = """
    <table><tr><td></td><td colspan="2">13.11. ПЕТАК</td>
    <td colspan="3">14.11. СУБОТА</td></tr>
    <tr><td colspan="6">Прогноза ажурирана: 13.11. 12:18</td></tr>
    <tr><td>Београд</td><td>25</td><td><img src="/prognoza/18.gif"></td>
    <td>5</td><td>9</td><td><img src="/prognoza/4.gif"></td></tr></table>"""
    now = datetime(2026, 11, 14, 7, 30, tzinfo=_ZONE)
    forecast = parse_forecast(html, now)
    assert forecast is not None and not forecast.hazardous
    assert forecast.text == "Днём до +9 °C, небольшая облачность."
    assert parse_forecast(html.replace("13.11. 12:18", "12.11. 12:18"), now) is None


def test_weather_warning_has_to_overlap_the_belgrade_day():
    alert = """<feed xmlns="http://www.w3.org/2005/Atom"
    xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2"><entry>
    <cap:areaDesc>Belgrade</cap:areaDesc><cap:event>Wind orange</cap:event>
    <cap:effective>2026-11-14T07:00:00+00:00</cap:effective>
    <cap:expires>2026-11-14T16:00:00+00:00</cap:expires></entry></feed>"""
    now = datetime(2026, 11, 14, 7, 30, tzinfo=_ZONE)
    assert parse_warning(alert.encode(), now) == (
        "РХМЗ предупреждает: сильный ветер, оранжевый уровень, 08:00–17:00."
    )
    assert parse_warning(alert.replace("Belgrade", "Novi Sad").encode(), now) is None
    assert parse_warning(alert.encode(), now.replace(hour=18)) is None


def test_render_uses_compact_html_one_hashtag_and_grouped_map():
    slot = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert slot is not None
    first = "event_0123456789abcdef1234"
    second = "event_0123456789abcdef1235"
    source = "https://www.beograd.rs/example"
    entries = [Entry("🚧 Перекрытия", "Улица <Центр>", source, map_id=first),
               Entry("🚋 Транспорт", "Линия 26", source),
               Entry("🌦 Погода", "Дождь", source),
               Entry("⚡ Свет", "Отключение", source, map_id=second)]
    assert render(slot, entries[:1]) is None
    digest = render(slot, entries, "https://t.me/relocant_media_belgrade_bot/map")
    assert digest is not None and digest.map_url is not None
    assert is_map_event_url(digest.map_url) and first in digest.map_url and second in digest.map_url
    assert "<b>Свет</b>" in digest.html and "⚡" not in digest.html
    assert "&lt;Центр&gt;" in digest.html and digest.html.count("#Сводка") == 1


def test_uncertain_send_is_recorded_and_never_repeated():
    slot = due_slot(datetime(2026, 11, 13, 19, 30, tzinfo=UTC))
    assert slot is not None
    digest = render(slot, [Entry("Новость", "", "https://t.me/eduvbelgrad/4"),
                           Entry("Другая новость", "", "https://t.me/eduvbelgrad/5")])
    assert digest is not None
    state = {}
    seen = []

    def commit(current, sha, updates):
        state.update(updates)
        seen.append(state["digest_receipts"][slot.key]["state"])
        return state, "updated"

    async def send():
        assert seen == ["sending"]
        raise TimeoutError

    receipt = asyncio.run(deliver(slot, digest, state, "base", commit, send))
    assert receipt["state"] == "uncertain"
    assert asyncio.run(deliver(slot, digest, state, "updated", commit, send)) == receipt
    assert seen == ["sending", "uncertain"]
