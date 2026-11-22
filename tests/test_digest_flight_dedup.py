from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.domain.flight_notices import flight_cancellation_key
from relocant_media.ops.belgrade_digests import Slot, evening_entries, render
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

NOW = datetime(2026, 11, 22, 19, 30, tzinfo=UTC)
POSTS = [
    (153, "Er Srbija отменила рейс из Белграда в Брюссель на 23 ноября",
     "Рейс отменён на 23 ноября. Пассажирам предложат альтернативы.", 5),
    (154, "Эр Сербия отменила рейс из Белграда в Брюссель из-за забастовки",
     "Если у вас куплен билет на понедельник, обратите внимание: рейс отменён.", 3),
    (155, "Air Serbia отменила рейсы между Белградом и Брюсселем на 23 ноября",
     "Все рейсы на понедельник, 23 ноября отменены. Пассажирам направят уведомления.", 2),
]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        value.add(Source(id="media", city="belgrade", name="Медиа",
                         url="https://example.com/", source_type="news_media", trust=1))
        value.commit()
        yield value
    engine.dispose()


def add_post(session, message_id, title, body, hours, *, category="transport"):
    raw = RawItem(source_id="media", external_id=str(message_id),
                  url=f"https://example.com/{message_id}", title="Исходный материал", body=body)
    post = Post(city="belgrade", category=category, title=title, body=body, confidence=1,
                status="ready", published_at=NOW - timedelta(hours=hours),
                telegram_message_id=message_id)
    session.add_all([raw, post])
    session.flush()
    session.add(EditorialRun(raw_item_id=raw.id, post_id=post.id, evidence={}, outcome={},
                             delivery_status="sent"))
    session.commit()
    return post


def test_real_three_public_headlines_become_one_latest_digest_entry(session):
    for args in POSTS:
        add_post(session, *args)
    entries = evening_entries(session, NOW)
    assert len(entries) == 1
    assert entries[0].url == "https://t.me/eduvbelgrad/155"
    assert entries[0].label == POSTS[-1][1]
    slot = Slot("evening", NOW, NOW + timedelta(minutes=45))
    # Один уникальный сюжет не заполняет штатный минимум сводки повторами.
    assert render(slot, entries) is None
    # Канонические отправленные посты и квитанции остаются на месте.
    assert session.query(Post).count() == 3
    assert all(run.delivery_status == "sent" for run in session.query(EditorialRun))


def test_duplicates_do_not_consume_the_four_event_slots(session):
    for args in POSTS:
        add_post(session, *args)
    for index in range(3):
        add_post(session, 200 + index, f"Новая городская история {index}",
                 "Самостоятельное событие района.", index + 1, category="city")
    entries = evening_entries(session, NOW)
    assert len(entries) == 4
    assert entries[0].url.endswith("/155")
    digest = render(Slot("evening", NOW, NOW + timedelta(minutes=45)), entries)
    assert digest is not None
    assert "/155" in digest.html and "/153" not in digest.html and "/154" not in digest.html
    assert {entry.url.rsplit("/", 1)[-1] for entry in entries[1:]} == {"200", "201", "202"}


@pytest.mark.parametrize("before,after", [
    ("23 ноября", "24 ноября"),
    ("23 ноября", "23 ноября 2027"),
    ("Брюсселем", "Парижем"),
    ("Белградом", "Берлином"),
    ("Air Serbia", "Lufthansa"),
    ("отменила", "возобновила"),
    ("отменила", "не отменила"),
])
def test_other_cancellation_or_changed_status_keeps_separate_entry(session, before, after):
    add_post(session, *POSTS[0])
    mid, title, body, hours = POSTS[-1]
    add_post(session, mid, title.replace(before, after), body.replace(before, after), hours)
    assert len(evening_entries(session, NOW)) == 2


def test_relative_weekday_uses_each_posts_date_instead_of_digest_date():
    title = "Air Serbia отменила рейс из Белграда в Брюссель"
    body = "Отмена на понедельник."
    first = flight_cancellation_key(title, body, published_at=NOW)
    next_week = flight_cancellation_key(title, body, published_at=NOW + timedelta(days=7))
    explicit = flight_cancellation_key(title, "Отмена на 23 ноября.", published_at=NOW)
    assert first is not None and first == explicit and first != next_week


def test_unknown_date_or_missing_timestamp_does_not_produce_digest_key():
    _, title, _, _ = POSTS[1]
    assert flight_cancellation_key(title, "Дата не указана.", published_at=NOW) is None
    assert flight_cancellation_key(title, "Отмена на понедельник.", published_at=None) is None


def test_digest_does_not_merge_carriers_when_related_story_hold_is_broader(monkeypatch):
    from relocant_media.domain import flight_notices

    # Hold вправе требовать проверку одной причины отмены для разных компаний;
    # сводка не вправе удалять отдельное последствие для другого перевозчика.
    monkeypatch.setattr(
        flight_notices, "_notice", lambda *a: ("flight_cancellation", "route", "day"))
    first = flight_cancellation_key(POSTS[-1][1], POSTS[-1][2], published_at=NOW)
    other = flight_cancellation_key(POSTS[-1][1].replace("Air Serbia", "Lufthansa"),
                                    POSTS[-1][2], published_at=NOW)
    unknown = flight_cancellation_key(POSTS[-1][1].replace("Air Serbia", "Неизвестный перевозчик"),
                                      POSTS[-1][2], published_at=NOW)
    assert first is not None and other is not None and first != other
    assert unknown is None
