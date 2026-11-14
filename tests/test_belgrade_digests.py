import asyncio
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.bot.formatting import TELEGRAM_TEXT_LIMIT, formatted_text_units
from relocant_media.collectors.rhmz_forecast import parse_forecast, parse_warning
from relocant_media.domain.map_links import is_map_event_url, map_event_id
from relocant_media.ops.belgrade_digests import (
    Entry,
    _evidence_sha,
    _incident_content,
    _link_to_published,
    _road_text,
    deliver,
    due_slot,
    render,
)
from relocant_media.ops.map_export import Address, Incident, PrintedRange, Segment, Window
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

_ZONE = ZoneInfo("Europe/Belgrade")


def test_two_week_slots_use_belgrade_time():
    first = due_slot(datetime(2026, 11, 13, 19, 30, tzinfo=UTC))
    assert first is not None and first.kind == "evening"
    assert due_slot(datetime(2026, 11, 13, 19, 29, tzinfo=UTC)) is None
    morning = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert morning is not None and morning.kind == "morning"
    assert due_slot(datetime(2026, 11, 14, 7, 15, tzinfo=UTC)) is None
    assert due_slot(datetime(2026, 11, 27, 6, 30, tzinfo=UTC)) is not None
    reminder = due_slot(datetime(2026, 11, 27, 19, 30, tzinfo=UTC))
    assert reminder is not None and reminder.kind == "reminder"


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
    assert render(slot, []) is None
    assert render(slot, entries[:1]) is not None
    digest = render(slot, entries, "https://t.me/relocant_media_belgrade_bot/map")
    assert digest is not None and digest.map_url is not None
    assert is_map_event_url(digest.map_url) and first in digest.map_url and second in digest.map_url
    assert "<b>Свет</b>" in digest.html and "⚡" not in digest.html
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
    text, rows = _incident_content(incident, now, now.replace(day=15, hour=0, minute=0))
    digest = render(slot, [Entry("⚡ Свет", text, "https://t.me/eduvbelgrad/102",
                                 "Подробнее", address_rows=rows)])
    assert digest is not None
    lead, quote = digest.html.split("<blockquote expandable>", 1)
    assert "https://t.me/eduvbelgrad/102" in lead and "MASARIKOVA" not in lead
    assert "MASARIKOVA, 2 — 08:00–13:00" in quote
    assert "BULEVAR MIHAJLA PUPINA, 6 — 08:00–12:00" in quote
    assert "MILENTIJA POPOVIĆA, 11-29, 33-43 — 10:00–16:00" in quote
    assert "33-43, 35" not in quote
    assert "MILUTINA MILANKOVIĆA, 1A-1K — 10:00–16:00" in quote
    assert "OLD" not in quote and "https://" not in quote


def test_long_address_quote_is_bounded_by_telegram_text_limit():
    slot = due_slot(datetime(2026, 11, 14, 6, 30, tzinfo=UTC))
    assert slot is not None
    rows = tuple(f"Улица <Центр>, {index} — 08:00–13:00" for index in range(250))
    digest = render(slot, [
        Entry("⚡ Свет", "Два района.", "https://www.beograd.rs/example", address_rows=rows),
        Entry("💧 Вода", "Чукарица.", "https://www.beograd.rs/water", address_rows=rows),
    ])
    assert digest is not None and formatted_text_units(digest.html) <= TELEGRAM_TEXT_LIMIT
    assert digest.html.count("<blockquote expandable>") == 2
    assert digest.html.count("Полный список — по ссылке выше.") == 2
    assert "&lt;Центр&gt;" in digest.html


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
        entry = Entry("⚡ Свет", "Два района.", source_url, evidence_sha=_evidence_sha(document))
        linked = _link_to_published(session, entry, now)
        assert linked.url == "https://t.me/eduvbelgrad/102" and linked.link_text == "Подробнее"
        assert _link_to_published(session, entry, now.replace(day=12)) == entry
        revision = RawItem(id=2, source_id=source.id, external_id="2", url=source_url,
                           title=item.title, body="12:00–15:00")
        session.add(revision)
        session.commit()
        current = Entry("⚡ Свет", "Два района.", source_url, evidence_sha=_evidence_sha(
            EvidenceDocument.model_validate(revision, from_attributes=True),
        ))
        assert _link_to_published(session, current, now) == current
    engine.dispose()


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
    notice = Entry("💧 Вода", "Бараево — 08:00–22:00.", "https://www.beograd.rs/example",
                   reminder=True, original=106)
    draft = render(slot, [notice])
    assert draft is not None
    assert "Напоминание на завтра · 17 ноября" in draft.html
    assert "https://t.me/eduvbelgrad/106" in draft.html
    assert "#Сводка" not in draft.html
