from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.domain.freshness import (
    draft_date_reason,
    freshness_decision,
    publication_date_context,
)

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.mark.parametrize("relative", ["sutra", "сутра", "завтра"])
def test_expired_relative_water_notice_is_discarded_before_ai(relative):
    published = datetime(2026, 10, 19, 12, tzinfo=UTC)
    now = datetime(2026, 10, 21, 6, tzinfo=UTC)
    decision = freshness_decision(
        "Радови", f"Потрошачи ће {relative} бити без воде.", published, now
    )
    assert decision.action == "discard" and decision.reason == "event_expired"


def test_relative_dates_use_belgrade_day_across_midnight():
    published = datetime(2026, 10, 19, 22, 30, tzinfo=UTC)  # 20 October locally
    now = datetime(2026, 10, 21, 6, tzinfo=UTC)
    assert freshness_decision("Объявление", "Sutra bez vode.", published, now).action == "keep"
    assert publication_date_context(published)["tomorrow_belgrade"] == "2026-10-21"


def test_publication_context_never_invents_unknown_dates():
    assert publication_date_context(None)["tomorrow_belgrade"] is None
    assert draft_date_reason("Новость", "Завтра отключат воду.")
    assert draft_date_reason("Новость", "21 октября отключат воду.") is None


@pytest.mark.parametrize(("title", "body", "reason"), [
    ("Отключение воды", "**Когда:** До `15:00`. Адрес уточняется.", True),
    ("Отключение воды", "Когда:\nДо 15:00.", True),
    ("Отключение воды", "Время работ: до 15.00.", True),
    ("Отключение воды", "Когда: до 15 часов.", True),
    ("Отключение воды", "Где: Земун.\nКогда: 18 ноября до 15:00.", False),
    ("Отключение воды", "Когда: 18-го ноября до 15:00.", False),
    ("Отключение воды", "Когда: 2026-11-18 с 8:00 до 15:00.", False),
    ("Отключение воды", "Когда: 18.11.2026.", False),
    ("Отключение воды 18 ноября", "Когда: с 8:00 до 15:00.", False),
    ("Отключение воды", "18 ноября отключат воду.\nКогда: до 15:00.", False),
    ("Отключение воды", "18 ноября воду отключили по другому адресу.\nКогда: до 15:00.", True),
    ("Отключение воды", "Работы затронут Земун.\nКогда: до 15:00.\nИсточник от 18 ноября.", True),
    ("Отключение воды 18 ноября", "19 ноября отключат воду.\nКогда: до 15:00.", True),
    ("Отключение воды 18 ноября", "Когда: 31 ноября до 15:00.", True),
    ("Отключение воды", "Когда: работы продлятся 24 часа.", False),
    ("Транспорт", "Запись сделана в 15:00.", False),
])
def test_clock_in_schedule_needs_a_calendar_day(title: str, body: str, reason: bool) -> None:
    assert draft_date_reason(title, body) == (
        "draft_requires_absolute_event_date" if reason else None
    )


def test_unrelated_tomorrow_does_not_expire_article():
    assert (
        freshness_decision(
            "Новость",
            "Гость сказал: завтра я вернусь.",
            datetime(2026, 10, 19, 12, tzinfo=UTC),
            datetime(2026, 10, 21, 6, tzinfo=UTC),
        ).action
        == "keep"
    )


@pytest.mark.parametrize(
    ("title", "body", "age", "expected"),
    [
        ("Златовласка", "Спектакль состоится 10 октября 2026", 10, "discard"),
        ("Ремонт", "Радови на стази у Цераку", 11, "discard"),
        ("Радови", "Радови од 20. октобра до 8. новембра 2026.", 1, "keep"),
        ("Спектакль", "Состоится 1 ноября 2026", 1, "keep"),
        ("Спектакль", "Состоится 1 ноября 2026", 8, "discard"),
        ("Спектакль", "Состоится 1 ноября 2026", 3, "keep"),
        ("Predstava", "Predstava 10. oktobra 2026.", 1, "discard"),
        ("Представа", "Представа 10. октобра 2026.", 1, "discard"),
        ("Концерт", "Концерт 10.10.2026", 1, "discard"),
        ("Радови", "Радови од 10. октобра до 8. новембра", 3, "keep"),
        ("Архив", "Основано 1 ноября 2026", 3, "discard"),
        ("Ремонт", "Работы обсуждаются. Архив от 1 ноября 2026", 3, "discard"),
        ("Концерт", "Концерт 31 октября 2026 и 2 ноября 2026", 3, "discard"),
        ("Концерт", "Концерт 40 октября 2026", 3, "discard"),
        ("Ремонт", "Работы начались 10 октября 2026", 1, "keep"),
        ("Ремонт", "Работы начались 1 ноября 2026", 3, "discard"),
        ("Ремонт", "Работы завершатся 10 октября 2026", 1, "discard"),
        ("Радови", "Радови од 5. октобра до 10. октобра 2026", 1, "discard"),
    ],
)
def test_freshness(title: str, body: str, age: int, expected: str) -> None:
    decision = freshness_decision(title, body, NOW - timedelta(days=age), NOW)
    assert decision.action == expected


def test_recent_expired_event_is_discarded_before_ai() -> None:
    decision = freshness_decision("Спектакль 10 октября", "", NOW - timedelta(hours=2), NOW)
    assert decision.action == "discard"
    assert decision.reason == "event_expired"


def test_public_channel_has_shorter_window() -> None:
    published = NOW - timedelta(hours=30)
    assert freshness_decision("Новость", "", published, NOW).action == "keep"
    assert freshness_decision("Новость", "", published, NOW, horizon_hours=24).action == "discard"


@pytest.mark.parametrize("published", [None, NOW.replace(tzinfo=None), NOW + timedelta(hours=1)])
def test_unreliable_publication_date_is_held(published: datetime | None) -> None:
    assert freshness_decision("Концерт", "1 ноября 2026", published, NOW).action == "hold"


def test_year_boundary_without_year_is_not_guessed() -> None:
    now = datetime(2026, 12, 30, 12, tzinfo=UTC)
    assert (
        freshness_decision("Концерт", "Концерт 2 января", now - timedelta(days=3), now).action
        == "discard"
    )
    assert (
        freshness_decision("Концерт", "Концерт 2 января 2027", now - timedelta(days=3), now).action
        == "keep"
    )


def test_age_limit_and_horizon_boundaries() -> None:
    assert freshness_decision("Новость", "", NOW - timedelta(hours=48), NOW).action == "keep"
    assert (
        freshness_decision("Концерт", "Концерт 1 ноября 2026", NOW - timedelta(days=7), NOW).action
        == "keep"
    )
    assert (
        freshness_decision(
            "Концерт", "Концерт 1 ноября 2026", NOW - timedelta(days=7, seconds=1), NOW
        ).action
        == "discard"
    )


def test_expiry_uses_belgrade_calendar_after_midnight() -> None:
    # 20 October 00:30 in Belgrade, still 19 October in UTC.
    now = datetime(2026, 10, 19, 22, 30, tzinfo=UTC)
    decision = freshness_decision(
        "Концерт", "Концерт 19 октября 2026", now - timedelta(hours=3), now
    )
    assert decision.action == "discard"
    assert decision.reason == "event_expired"


def test_spaced_numeric_concert_date_preserves_future_and_discards_expired() -> None:
    published = NOW - timedelta(days=3)
    assert freshness_decision('Концерт 31. 10. 2026.', '', published, NOW).action == 'keep'
    assert freshness_decision('Концерт 10. 10. 2026.', '', NOW, NOW).reason == 'event_expired'


@pytest.mark.parametrize(("lead", "expected"), [
    ("Kreator i Overkill nastupaju u sredu, 25. novembra, u Hangaru.",
     "dated_event_still_relevant"),
    ("Музичари наступају 25. новембра у Београду.", "dated_event_still_relevant"),
    ("Музыканты выступят 25 ноября в Белграде.", "dated_event_still_relevant"),
    ("Kreator i Overkill nastupaju 21. novembra u Hangaru.", "event_expired"),
    ("Kreator i Overkill nastupali su 25. novembra prošle godine.",
     "publication_outside_news_window"),
    ("Kreator i Overkill nastupaju 25. ili 26. novembra i 28. novembra.",
     "publication_outside_news_window"),
])
def test_performance_announcements_need_an_explicit_unambiguous_date(lead, expected):
    published = datetime(2026, 11, 20, 14, 31, tzinfo=UTC)
    now = datetime(2026, 11, 24, 9, tzinfo=UTC)
    assert freshness_decision("U Hangaru", lead, published, now).reason == expected
