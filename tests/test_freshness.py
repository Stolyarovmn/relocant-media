from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.domain.freshness import freshness_decision

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


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
    decision = freshness_decision(
        "Спектакль 10 октября", "", NOW - timedelta(hours=2), NOW
    )
    assert decision.action == "discard"
    assert decision.reason == "event_expired"


def test_public_channel_has_shorter_window() -> None:
    published = NOW - timedelta(hours=30)
    assert freshness_decision("Новость", "", published, NOW).action == "keep"
    assert freshness_decision(
        "Новость", "", published, NOW, horizon_hours=24
    ).action == "discard"


@pytest.mark.parametrize("published", [None, NOW.replace(tzinfo=None), NOW + timedelta(hours=1)])
def test_unreliable_publication_date_is_held(published: datetime | None) -> None:
    assert freshness_decision("Концерт", "1 ноября 2026", published, NOW).action == "hold"


def test_year_boundary_without_year_is_not_guessed() -> None:
    now = datetime(2026, 12, 30, 12, tzinfo=UTC)
    assert freshness_decision(
        "Концерт", "Концерт 2 января", now - timedelta(days=3), now
    ).action == "discard"
    assert freshness_decision(
        "Концерт", "Концерт 2 января 2027", now - timedelta(days=3), now
    ).action == "keep"


def test_age_limit_and_horizon_boundaries() -> None:
    assert freshness_decision("Новость", "", NOW - timedelta(hours=48), NOW).action == "keep"
    assert freshness_decision(
        "Концерт", "Концерт 1 ноября 2026", NOW - timedelta(days=7), NOW
    ).action == "keep"
    assert freshness_decision(
        "Концерт", "Концерт 1 ноября 2026", NOW - timedelta(days=7, seconds=1), NOW
    ).action == "discard"


def test_expiry_uses_belgrade_calendar_after_midnight() -> None:
    # 20 October 00:30 in Belgrade, still 19 October in UTC.
    now = datetime(2026, 10, 19, 22, 30, tzinfo=UTC)
    decision = freshness_decision(
        "Концерт", "Концерт 19 октября 2026", now - timedelta(hours=3), now
    )
    assert decision.action == "discard"
    assert decision.reason == "event_expired"
