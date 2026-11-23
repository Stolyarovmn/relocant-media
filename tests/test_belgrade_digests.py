import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_transport_map import _map_html
from test_utility_scope import document as power_document

from relocant_media.ai.models import EvidenceDocument
from relocant_media.bot.formatting import TELEGRAM_TEXT_LIMIT, formatted_text_units
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.collectors.rhmz_forecast import (
    FORECAST_URL,
    WARNING_URL,
    _forecast_reading,
    collect_weather,
    parse_forecast,
    parse_warning,
)
from relocant_media.domain.map_links import is_map_event_url, map_event_id
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops import belgrade_digests
from relocant_media.ops.belgrade_digests import (
    Entry,
    _evidence_sha,
    _incident_content,
    _link_to_published,
    _road_text,
    deliver,
    due_slot,
    morning_delivery,
    render,
)
from relocant_media.ops.map_export import (
    Address,
    Incident,
    PrintedRange,
    Segment,
    WaterDistrict,
    Window,
)
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

_ZONE = ZoneInfo("Europe/Belgrade")


def test_two_week_slots_use_belgrade_time():
    first = due_slot(datetime(2026, 11, 13, 19, 30, tzinfo=UTC))
    assert first is not None and first.kind == "evening"
    assert due_slot(datetime(2026, 11, 13, 19, 29, tzinfo=UTC)) is None
    morning = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert morning is not None and morning.kind == "morning"
    # После 45-минутного окна утренний слот остаётся догоняющим до 11:00 (#321).
    assert due_slot(datetime(2026, 11, 14, 7, 15, tzinfo=UTC)).kind == "morning"
    assert due_slot(datetime(2026, 11, 14, 10, 0, tzinfo=UTC)) is None
    assert due_slot(datetime(2026, 11, 27, 6, 30, tzinfo=UTC)) is not None


def test_digests_continue_after_the_pilot_without_an_end_date():
    """Owner decision 21.11.2026 (#321): the pilot end no longer stops digests."""
    assert belgrade_digests._policy().ends_before is None
    for now, kind in (
        (datetime(2026, 11, 27, 19, 30, tzinfo=UTC), "evening"),
        (datetime(2026, 11, 28, 6, 30, tzinfo=UTC), "morning"),
        (datetime(2027, 1, 15, 6, 30, tzinfo=UTC), "morning"),
        # Summer time: 07:30 and 20:30 in Belgrade are 05:30 and 18:30 UTC.
        (datetime(2027, 7, 1, 5, 30, tzinfo=UTC), "morning"),
        (datetime(2027, 7, 1, 18, 30, tzinfo=UTC), "evening"),
    ):
        slot = due_slot(now)
        assert slot is not None and slot.kind == kind
    assert due_slot(datetime(2027, 1, 15, 5, 30, tzinfo=UTC)) is None
    assert belgrade_digests.digest_end_warning(datetime(2027, 1, 15, 6, 30, tzinfo=UTC)) is None


def test_missed_morning_digest_catches_up_until_eleven():
    # #321: 21.11 every cycle failed 04:20–10:35 UTC and the morning digest was lost.
    caught = due_slot(datetime(2026, 11, 21, 9, 40, tzinfo=UTC))  # 10:40 Belgrade
    assert caught is not None and caught.kind == "morning"
    assert caught.scheduled.hour == 7 and caught.scheduled.minute == 30
    assert caught.key == due_slot(datetime(2026, 11, 21, 6, 30, tzinfo=UTC)).key
    assert due_slot(datetime(2026, 11, 21, 10, 0, tzinfo=UTC)) is None  # 11:00 Belgrade


def test_explicit_end_date_still_stops_regular_digests(monkeypatch):
    policy = belgrade_digests._policy()
    monkeypatch.setattr(belgrade_digests, "_policy", lambda: policy.model_copy(update={
        "ends_before": datetime(2026, 11, 27, 20, 30),
    }))
    assert due_slot(datetime(2026, 11, 27, 6, 30, tzinfo=UTC)).kind == "morning"
    assert due_slot(datetime(2026, 11, 28, 6, 30, tzinfo=UTC)) is None
    assert morning_delivery({"status": "ready"}, datetime(2026, 11, 28, 12, tzinfo=UTC)) is None
    reminder = due_slot(datetime(2026, 11, 27, 19, 30, tzinfo=UTC))
    assert reminder is not None and reminder.kind == "reminder"


@pytest.mark.parametrize(("month", "utc_hour"), [(7, 18), (11, 19)])
def test_digest_end_warning_uses_belgrade_time(month, utc_hour, monkeypatch):
    end = datetime(2027, month, 27, 20, 30)
    policy = belgrade_digests._policy().model_copy(update={"ends_before": end})
    monkeypatch.setattr(belgrade_digests, "_policy", lambda: policy)
    threshold = datetime(2027, month, 24, utc_hour, 30, tzinfo=UTC)
    assert belgrade_digests.digest_end_warning(threshold - timedelta(seconds=1)) is None
    for now in (threshold, threshold + timedelta(days=4)):
        assert belgrade_digests.digest_end_warning(now) == end.replace(tzinfo=_ZONE)


@pytest.mark.parametrize("cutoff", [
    datetime(2026, 11, 22, 10, tzinfo=UTC),
    datetime(2027, 7, 1, 9, tzinfo=UTC),
])
def test_morning_check_uses_the_end_of_the_same_local_slot(cutoff):
    from datetime import timedelta

    assert morning_delivery({"status": "ready"}, cutoff - timedelta(seconds=1)) is None
    delivery = morning_delivery({"status": "ready"}, cutoff)
    assert delivery is not None and delivery.slot.expires.hour == 11
    assert (delivery.status, delivery.reason) == ("missed", "no_recorded_result")


@pytest.mark.parametrize(("receipt", "status", "reason"), [
    ({"state": "sent", "message_id": 152}, "sent", "confirmed"),
    ({"state": "sent", "message_id": True}, "unconfirmed", "invalid_receipt"),
    ({"state": "uncertain"}, "unconfirmed", "telegram_unconfirmed"),
    ({"state": "sending"}, "unconfirmed", "telegram_unconfirmed"),
])
def test_morning_watch_checks_exact_receipt_for_that_day(receipt, status, reason):
    now = datetime(2026, 11, 22, 10, tzinfo=UTC)
    state = {"status": "busy", "digest_receipts": {
        "belgrade:2026-11-22:morning": receipt,
        "belgrade:2026-11-21:morning": {"state": "sent", "message_id": 151},
    }, belgrade_digests.MORNING_ATTEMPT: {
        "slot": "belgrade:2026-11-22:morning", "reason": "insufficient_items",
    }}
    delivery = morning_delivery(state, now)
    assert (delivery.status, delivery.reason) == (status, reason)
    del state["digest_receipts"]["belgrade:2026-11-22:morning"]
    assert morning_delivery(state, now).reason == "insufficient_items"
    assert morning_delivery(state, now.replace(day=23)).reason == "no_recorded_result"


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
    assert _forecast_reading(html.replace("13.11. 12:18", "12.11. 12:18"), now) == (None, "stale")
    assert _forecast_reading(html.replace("14.11. СУБОТА", "15.11. НЕДЕЉА"), now) == (
        None, "today_missing",
    )
    assert _forecast_reading(html.replace("/prognoza/4.gif", "missing"), now) == (
        None, "invalid_payload",
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("feed", "status", "has_warning"), [
    ("", "none_active", False),
    ("<entry/>", "invalid_payload", False),
    ("<entry><cap:areaDesc>Belgrade</cap:areaDesc></entry>", "invalid_payload", False),
    ("<entry><cap:areaDesc>Belgrade</cap:areaDesc><cap:event>Wind orange</cap:event>"
     "<cap:effective>2026-11-14T07:00:00+00:00</cap:effective>"
     "<cap:expires>2026-11-14T16:00:00+00:00</cap:expires></entry><entry/>",
     "partial", True),
])
async def test_weather_accounts_for_missing_and_malformed_alerts(feed, status, has_warning):
    now = datetime(2026, 11, 14, 6, 30, tzinfo=UTC)

    def respond(request):
        if str(request.url) == FORECAST_URL:
            raise httpx.ReadTimeout("unavailable", request=request)
        return httpx.Response(200, text=(
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">'
            f'{feed}</feed>'
        ))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        weather = await collect_weather(client, now)
    assert weather.forecast is None
    assert bool(weather.warning) is has_warning
    assert weather.statuses() == {"rhmz_forecast": "transport_error", "meteoalarm": status}


def test_render_uses_compact_html_one_hashtag_and_grouped_map():
    slot = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert slot is not None
    first = "event_0123456789abcdef1234"
    second = "event_0123456789abcdef1235"
    source = "https://www.beograd.rs/example"
    entries = [Entry("🚧 Перекрытия", "Улица <Центр>", source, map_id=first),
               Entry("🚋 Транспорт", "Линия 26", source),
               Entry("🌦 Погода", "Дождь", source),
               Entry("⚡ Отключения света", "Отключение", source, map_id=second)]
    assert render(slot, []) is None
    assert render(slot, entries[:1]) is not None
    digest = render(slot, entries, "https://t.me/relocant_media_belgrade_bot/map")
    assert digest is not None and digest.map_url is not None
    assert is_map_event_url(digest.map_url) and first in digest.map_url and second in digest.map_url
    assert "<b>Отключения света</b>" in digest.html and "⚡" not in digest.html
    assert "&lt;Центр&gt;" in digest.html and digest.html.count("#Сводка") == 1


def test_long_roadworks_keep_the_end_year():
    source = "https://www.bgprevoz.rs/linije/aktuelne-izmene/1153"
    segment = Segment(municipality="Novi Beograd", street="Zemunski put",
                      start="Sajmište", end="Zarije Vujoševića")
    incident = Incident(id=map_event_id(source), kind="road", title="Работы", source_url=source,
                        starts_on=date(2026, 11, 14), ends_on=date(2027, 9, 2), segments=(segment,))
    assert _road_text(incident, incident.segments).endswith("по 2 сентября 2027 года.")


def test_morning_power_notice_keeps_every_current_address_and_its_interval():
    now = datetime(2026, 11, 14, 7, 30, tzinfo=_ZONE)
    slot = due_slot(now)
    assert slot is not None
    source = "https://www.beograd.rs/example"
    intervals = [Window(starts_at=now.replace(hour=start, minute=0),
                        ends_at=now.replace(hour=end, minute=0))
                 for start, end in ((8, 13), (8, 12), (10, 16), (5, 7))]
    first, second, third, expired = (window.model_dump() for window in intervals)
    incident = Incident(
        id=map_event_id(source), kind="power", title="Плановое отключение", source_url=source,
        windows=tuple(intervals),
        addresses=(
            Address(**first, municipality="Savski venac", street="MASARIKOVA", number="2"),
            Address(**second, municipality="Novi Beograd", street="BULEVAR MIHAJLA PUPINA",
                    number="6"),
            Address(**third, municipality="Novi Beograd", street="MILENTIJA POPOVIĆA",
                    number="35"),
            Address(**expired, municipality="Novi Beograd", street="OLD", number="6"),
        ),
        ranges=(
            PrintedRange(**third, municipality="Novi Beograd", street="MILENTIJA POPOVIĆA",
                         number="11-29, 33-43"),
            PrintedRange(**third, municipality="Novi Beograd", street="MILUTINA MILANKOVIĆA",
                         number="1A-1K"),
        ),
    )
    policy = belgrade_digests._policy()
    text, rows = _incident_content(incident, now, now.replace(day=15, hour=0, minute=0), policy)
    digest = render(slot, [Entry("⚡ Отключения света", text, "https://t.me/eduvbelgrad/102",
                                 "Подробнее", address_rows=rows)])
    assert digest is not None
    lead, quote = digest.html.split("<blockquote expandable>", 1)
    assert "Новый Белград, Савски-Венац." in lead
    assert "https://t.me/eduvbelgrad/102" in lead and "MASARIKOVA" not in lead
    assert "Савски-Венац, MASARIKOVA, 2 — 08:00–13:00" in quote
    assert "Новый Белград, BULEVAR MIHAJLA PUPINA, 6 — 08:00–12:00" in quote
    assert "MILENTIJA POPOVIĆA, 11-29, 33-43 — 10:00–16:00" in quote
    assert "33-43, 35" not in quote
    assert "MILUTINA MILANKOVIĆA, 1A-1K — 10:00–16:00" in quote
    assert "OLD" not in quote and "https://" not in quote


def test_power_summary_groups_suburbs_under_unique_municipalities():
    now = datetime(2026, 11, 19, 7, 30, tzinfo=_ZONE)
    tomorrow = datetime(2026, 11, 20, tzinfo=_ZONE)
    source = "https://www.beograd.rs/example"
    first = Window(starts_at=now.replace(hour=8, minute=0),
                   ends_at=now.replace(hour=13, minute=0))
    second = Window(starts_at=now.replace(hour=9, minute=0),
                    ends_at=now.replace(hour=14, minute=0))
    addresses = (
        Address(**first.model_dump(), municipality="Zemun", street="ПРВОМАЈСКА", number="4"),
        Address(**second.model_dump(), municipality="Земун, насеље ALTINA",
                street="UGRINOVAČKI PUT", number="12"),
        Address(**first.model_dump(), municipality="Zvezdara, naselje MIRIJEVO",
                street="MATICE SRPSKE", number="16"),
        Address(**first.model_dump(), municipality="Čukarica, naselje ŽARKOVO",
                street="TRGOVAČKA", number="8"),
        Address(**second.model_dump(), municipality="Палилула", street="ВИШЊИЧКА", number="12"),
        Address(**second.model_dump(), municipality="Novi Beograd", street="NEHRUOVA", number="2"),
    )
    incident = Incident(id=map_event_id(source), kind="power", title="Плановое отключение",
                        source_url=source, windows=(first, second), addresses=addresses)
    text, rows = _incident_content(incident, now, tomorrow, belgrade_digests._policy())
    assert text == "Земун, Звездара, Чукарица и ещё 2 района."
    assert len(rows) == len(addresses)
    assert "Земун, ALTINA, UGRINOVAČKI PUT, 12 — 09:00–14:00" in rows
    assert "Новый Белград, NEHRUOVA, 2 — 09:00–14:00" in rows
    assert all("naselje" not in row and "насеље" not in row for row in rows)


def test_water_notice_keeps_each_district_with_its_own_hours():
    now = datetime(2026, 11, 24, 7, 30, tzinfo=_ZONE)
    tomorrow = datetime(2026, 11, 25, tzinfo=_ZONE)
    source = "https://www.beograd.rs/example"
    start = now.replace(hour=8, minute=0)
    first = Window(starts_at=start, ends_at=now.replace(hour=15, minute=0))
    second = Window(starts_at=start, ends_at=now.replace(hour=16, minute=0))
    vozdovac = {**second.model_dump(), "municipality": "Voždovac"}
    notice = Incident(
        id=map_event_id(source), kind="water", title="Плановое отключение воды", source_url=source,
        windows=(first, second), water_districts=(
            WaterDistrict(**first.model_dump(), municipality="Savski venac",
                          boundary=("Fabrisova", "Zvečanska", "Oblakovska")),
            WaterDistrict(**vozdovac, whole_streets=("Rastka Petrovića",)),
        ), ranges=(PrintedRange(**vozdovac, street="Brane Ćosića", number="16–42"),),
        addresses=(Address(**vozdovac, street="Rastka Petrovića", number="37"),),
        segments=(Segment(municipality="Voždovac", street="Medakovićeva",
                          start="Zaplanjska", end="Katarine Ivanović"),),
    )
    policy = belgrade_digests._policy()
    text, rows = _incident_content(notice, now, tomorrow, policy)
    assert text == "Савски-Венац, Вождовац."
    assert rows == (
        "Савски-Венац, квартал между улицами Fabrisova, Zvečanska, Oblakovska — 08:00–15:00",
        "Вождовац, Rastka Petrovića — 08:00–16:00",
        "Вождовац, Brane Ćosića, 16–42 — 08:00–16:00",
        "Вождовац, Medakovićeva, от Zaplanjska до Katarine Ivanović — 08:00–16:00",
    )
    assert _incident_content(notice, now.replace(hour=15), tomorrow, policy) == (
        "Вождовац.", rows[1:],
    )


def test_long_address_quote_is_bounded_by_telegram_text_limit():
    slot = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert slot is not None
    rows = tuple(f"Улица <Центр>, {index} — 08:00–13:00" for index in range(250))
    digest = render(slot, [
        Entry("⚡ Отключения света", "Два района.", "https://www.beograd.rs/example",
              address_rows=rows),
        Entry("💧 Отключения воды", "Чукарица.", "https://www.beograd.rs/water", address_rows=rows),
    ])
    assert digest is not None and formatted_text_units(digest.html) <= TELEGRAM_TEXT_LIMIT
    assert digest.html.count("<blockquote expandable>") == 2
    assert digest.html.count("Полный список — по ссылке выше.") == 2
    assert "&lt;Центр&gt;" in digest.html


def test_evening_cover_keeps_long_address_lists_reachable():
    slot = due_slot(datetime(2026, 11, 16, 19, 30, tzinfo=UTC))
    assert slot is not None
    app = "https://t.me/relocant_media_belgrade_bot"
    event = "event_6b93722844d60e041820"
    rows = tuple(f"Улица {index}, дом 125 — 08:30–12:00" for index in range(100))
    entries = [
        Entry("💧 Отключения воды", "Бараево — 08:00–22:00.", "https://www.beograd.rs/water",
              reminder=True, address_rows=("Посёлки: Гай и Бождаревац",)),
        Entry("⚡ Отключения света", "Плановые отключения в нескольких общинах.",
              "https://t.me/eduvbelgrad/120", "Подробнее", map_id=event,
              reminder=True, address_rows=rows),
        Entry("Новости города", "Обычная новость за день.", "https://t.me/eduvbelgrad/122",
              "Подробнее"),
    ]
    original = render(slot, entries, app)
    assert original is not None and formatted_text_units(original.html) > 1024
    digest, photo = belgrade_digests._with_cover(slot, entries, original, app)
    assert photo is not None and photo.name == "evening-04.jpg"
    assert formatted_text_units(digest.html) <= 1024
    assert "Посёлки: Гай и Бождаревац" in digest.html
    assert 'href="https://t.me/eduvbelgrad/120">Подробнее' in digest.html
    assert f'href="{app}?startapp={event}">Все адреса' in digest.html
    assert "Фото:" in digest.html and digest.map_url == original.map_url

    unlinked = [entries[0], Entry("⚡ Отключения света", "", "https://www.beograd.rs/light",
                                  address_rows=rows), entries[2]]
    original = render(slot, unlinked, app)
    assert original is not None
    assert belgrade_digests._with_cover(slot, unlinked, original, app) == (original, None)


def test_morning_link_uses_only_a_sent_post_for_the_current_notice():
    now = datetime(2026, 11, 14, 7, 30, tzinfo=_ZONE)
    source_url = "https://www.beograd.rs/example"
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(id="city_beograd_news", city="belgrade", name="Город",
                        url=source_url, source_type="official", trust=1)
        session.add(source)
        session.flush()
        item = RawItem(id=1, source_id=source.id, external_id="1", url=source_url,
                       title="Отключение", body="08:00–13:00")
        post = Post(id=1, city="belgrade", title="Отключение", body="Опубликованный текст",
                    category="utilities", status="ready", confidence=1,
                    published_at=now.replace(day=13), telegram_message_id=102)
        session.add_all([item, post])
        session.flush()
        run = EditorialRun(raw_item_id=item.id, post_id=post.id, outcome={},
                           evidence={"title": item.title, "body": item.body},
                           delivery_status="sent")
        session.add(run)
        session.commit()
        document = EvidenceDocument.model_validate(item, from_attributes=True)
        entry = Entry("⚡ Отключения света", "Два района.", source_url,
                      evidence_sha=_evidence_sha(document))
        linked = _link_to_published(session, entry, now)
        assert linked.url == "https://t.me/eduvbelgrad/102" and linked.link_text == "Подробнее"
        assert _link_to_published(session, entry, now.replace(day=12)) == entry
        revision = RawItem(id=2, source_id=source.id, external_id="2", url=source_url,
                           title=item.title, body="12:00–15:00")
        session.add(revision)
        session.commit()
        current = Entry(
            "⚡ Отключения света", "Два района.", source_url,
            evidence_sha=_evidence_sha(EvidenceDocument.model_validate(
                revision, from_attributes=True)),
        )
        assert _link_to_published(session, current, now) == current
    engine.dispose()


def test_next_morning_delivers_addresses_and_all_map_items_before_evening_reminder(
    tmp_path, monkeypatch,
):
    now = datetime(2026, 11, 16, 7, 30, tzinfo=_ZONE)
    slot = due_slot(now)
    assert slot is not None and slot.kind == "morning"
    assert date(2026, 11, 17) in belgrade_digests._policy().official_reminder_sources
    app = "https://t.me/relocant_media_belgrade_bot"
    transport = "https://www.bgprevoz.rs/linije/aktuelne-izmene/"
    power = power_document(
        "Vračar 9.00 - 11.00 PRVA ULICA: 42,\n"
        "Zvezdara 13.00 - 15.00 DRUGA ULICA: 2,",
        title="ELEKTRIČNA ENERGIJA: Planirana isključenja za 16. novembar 2026. godine",
        published_at=now.replace(day=15),
    )
    period = "Период измене: 14.11.2026–30.11.2026.\n"
    road = CollectedItem(
        source_id="bgprevoz_changes", url=transport + "1501", external_id="1501", published_at=None,
        title="Радови у улици", body=period + "Затварање улице Прве.",
        source_html=_map_html(notice_id=1501, line=True),
    )
    route = CollectedItem(
        source_id="bgprevoz_changes", url=transport + "1502", external_id="1502", published_at=None,
        title="Линија бр. 60", body=period + "Затварање улице Друге. Кружна линија.",
        source_html=_map_html(notice_id=1502, line=True),
    )
    # An official line can lack textual endpoints, while a verified Russian brief
    # supplies the human-readable restriction in the digest.
    policy = belgrade_digests._policy()
    brief = belgrade_digests.TransportBrief(text="Перекрыта Первая улица.", details=())
    monkeypatch.setattr(belgrade_digests, "_policy", lambda: policy.model_copy(update={
        "transport_briefs": {_evidence_sha(EvidenceDocument.model_validate(
            road, from_attributes=True,
        )): brief},
    }))
    db = tmp_path / "digest.sqlite3"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for source_id in (power.source_id, road.source_id):
            sync_source(session, "belgrade", load_source_file(
                Path("cities/belgrade/sources") / f"{source_id}.yaml",
            ))
        capture_collected_batch(session, [
            CollectedItem(**power.model_dump(), external_id="power-16"), road, route,
        ])
        for item in session.scalars(select(RawItem)):
            item.ingested_at = now.replace(day=15)
        raw = session.scalar(select(RawItem).where(RawItem.url == power.url))
        assert raw is not None
        post = Post(city="belgrade", title="Отключение света", body="Список домов",
                    category="utilities", status="ready", confidence=1,
                    published_at=now.replace(day=15), telegram_message_id=201)
        session.add(post)
        session.flush()
        session.add(EditorialRun(raw_item_id=raw.id, post_id=post.id, delivery_status="sent",
                                 outcome={}, evidence={"title": raw.title, "body": raw.body}))
        session.commit()
    engine.dispose()

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(belgrade_digests, "datetime", FrozenDateTime)
    monkeypatch.setattr(belgrade_digests, "_cover", lambda slot: None)
    real_client = httpx.AsyncClient
    sent = []

    def telegram(request):
        if str(request.url) in {FORECAST_URL, WARNING_URL}:
            return httpx.Response(404)
        assert request.url.host == "api.telegram.org" and request.url.path.endswith("/sendMessage")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 202}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(telegram), **kwargs,
    ))
    state = {}

    def commit(current, sha, updates):
        state.update(updates)
        return state, "updated"

    receipt = asyncio.run(belgrade_digests.run(db, slot, state, "base", commit, {
        "TELEGRAM_BOT_TOKEN": "test-token", "RELOCANT_MAP_TELEGRAM_APP": app,
    }))
    assert receipt["state"] == "sent" and len(sent) == 1
    assert receipt["weather_sources"] == {"rhmz_forecast": "http_error", "meteoalarm": "http_error"}
    message = sent[0]
    html = message["text"]
    assert "<b>⚡ Отключения света</b>" in html
    assert '<a href="https://t.me/eduvbelgrad/201">Подробнее</a>' in html
    assert "<blockquote expandable><b>Адреса и время работ</b>" in html
    assert "PRVA ULICA, 42 — 09:00–11:00" in html
    assert "DRUGA ULICA, 2 — 13:00–15:00" in html
    button = message["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Посмотреть на карте"
    expected_ids = {map_event_id(item.url) for item in (road, route, power)}
    assert button["url"].startswith(app + "?startapp=digest_")
    assert set(button["url"].split("digest_", 1)[1].split("-")) == expected_ids


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


@pytest.mark.parametrize(("priority", "offset", "count", "expected"), [
    (False, 5, 2, "sent"),
    (True, 5, 2, "reminder_source_unconfirmed"),
    (False, 5, 1, "reminder_source_unconfirmed"),
    (True, 31, 2, "sent"),
])
@pytest.mark.asyncio
async def test_optional_unconfirmed_reminder_does_not_delay_a_ready_evening_digest(
    tmp_path, monkeypatch, priority, offset, count, expected,
):
    scheduled = datetime(2026, 11, 22, 19, 30, tzinfo=UTC)
    slot = due_slot(scheduled)
    assert slot is not None and slot.kind == "evening"
    now = scheduled + timedelta(minutes=offset)
    current_policy = belgrade_digests._policy()
    candidate = Entry("Отключение воды", "Объявление на завтра.",
                      "https://www.beograd.rs/notice")
    tomorrow = slot.scheduled.date() + timedelta(days=1)
    policy = current_policy.model_copy(update={
        "official_reminder_sources": {tomorrow: (candidate.url,)} if priority else {},
    })
    monkeypatch.setattr(belgrade_digests, "_policy", lambda: policy)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(belgrade_digests, "datetime", Clock)
    entries = [Entry(f"Материал {index}", "", f"https://t.me/eduvbelgrad/{index}")
               for index in range(1, count + 1)]
    monkeypatch.setattr(belgrade_digests, "evening_entries", lambda *_: entries)
    monkeypatch.setattr(belgrade_digests, "_tomorrow_entries", lambda *_: [candidate])
    confirm = AsyncMock(return_value=None)
    monkeypatch.setattr(belgrade_digests, "_confirm_reminder", confirm)
    monkeypatch.setattr(belgrade_digests, "_with_cover", lambda _, _entries, digest, _app: (
        digest, None,
    ))
    bot = Mock(send_digest_message=AsyncMock(return_value=200))
    monkeypatch.setattr(belgrade_digests, "TelegramBotAPI", lambda *_: bot)
    database = tmp_path / "editorial.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()
    state = {}

    def commit(current, sha, updates):
        state.update(updates)
        return state, "saved"

    outcome = await belgrade_digests.run(database, slot, state, "base", commit, {
        "TELEGRAM_BOT_TOKEN": "test-token",
    })
    assert outcome["state"] == expected
    confirm.assert_awaited_once()
    if expected == "sent":
        bot.send_digest_message.assert_awaited_once()
        message = bot.send_digest_message.await_args.args[1]
        assert "Главное за день" in message
        assert "Материал 1" in message and "Материал 2" in message
        assert candidate.url not in message
    else:
        bot.send_digest_message.assert_not_awaited()
        assert not state


def test_baraevo_near_date_keeps_the_published_scope():
    from relocant_media.services.water_outage_scope import (
        reported_water_places,
        water_outage_window,
    )

    document = EvidenceDocument(
        source_id="city_beograd_news",
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116643/Delovi-Barajeva-u-utorak-bez-vode.html",
        title="Delovi Barajeva u utorak bez vode",
        body=(
            "Planirani radovi na vodovodnoj mreži u opštini Barajevo prouzrokovaće u utorak, "
            "17. novembra, od 8 do 22 sata prestanak vodosnabdevanja. "
            "Bez vode će biti potrošači u naseljima Gaj, Ravni gaj, Boždarevac, Lisović, "
            "Beljina, Arnajevo i Rožanci, kao i ulicama Svetosavskoj i Srednjokrajskoj."
        ),
        published_at=datetime(2026, 11, 13, tzinfo=_ZONE),
        published_at_precision="day",
    )
    assert water_outage_window(document) == (
        datetime(2026, 11, 17, 8, tzinfo=_ZONE), datetime(2026, 11, 17, 22, tzinfo=_ZONE),
    )
    scope = reported_water_places(document)
    assert scope is not None and scope[0] == "Barajevo" and len(scope[1]) == 7
    assert water_outage_window(document.model_copy(update={"published_at": None})) is None
    cancelled = document.model_copy(update={"body": document.body + " Otkazani."})
    assert water_outage_window(cancelled) is None


def test_reminder_in_quiet_evening_can_publish_alone_with_original_link():
    slot = due_slot(datetime(2026, 11, 16, 19, 30, tzinfo=UTC))
    assert slot is not None
    notice = Entry("💧 Отключения воды", "Бараево — 08:00–22:00.", "https://www.beograd.rs/example",
                   reminder=True, original=106)
    draft = render(slot, [notice])
    assert draft is not None
    assert "Напоминание на завтра · 17 ноября" in draft.html
    assert "https://t.me/eduvbelgrad/106" in draft.html
    assert "#Сводка" not in draft.html


def test_only_standalone_reminder_replies_to_its_original_post():
    from datetime import timedelta

    from relocant_media.ops.belgrade_digests import Entry, Slot, reply_target

    when = datetime(2026, 11, 21, 19, 30, tzinfo=UTC)
    entry = Entry(label="🎭", text="Фламенко завтра", url="https://example.com/a")
    originals = {"https://example.com/a": 141}
    reminder = Slot("reminder", when, when + timedelta(minutes=45))
    evening = Slot("evening", when, when + timedelta(minutes=45))
    assert reply_target(reminder, [entry], originals) == 141
    # «Главное за день» с одним напоминанием — свой пост, без чужого заголовка сверху.
    assert reply_target(evening, [entry], originals) is None
    assert reply_target(reminder, [entry, entry], originals) is None
