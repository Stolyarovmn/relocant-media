from datetime import UTC, datetime

import pytest

from relocant_media.services.event_eligibility import event_content_reason

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.mark.parametrize("text", [
    "Radionica 21. oktobra 2026. u muzeju",
    "Спектакль 21 октября в 18:00",
    "Concert 21 October 2026",
    "Изложба 21.10.2026.",
    "Opening 2026-10-21",
    "Изложба 19.10.2026–25.10.2026",
])
def test_future_event(text: str) -> None:
    assert event_content_reason("Анонс", text, now=NOW) is None


def test_expired_event() -> None:
    assert event_content_reason("Анонс", "10. oktobra 2026", now=NOW) == "venue_event_expired"


@pytest.mark.parametrize("text", ["Скидка 20%", "Свежий анонс", "31.02.2027"])
def test_no_usable_event_date(text: str) -> None:
    assert event_content_reason("Анонс", text, now=NOW) == "venue_event_date_missing"


def test_no_implicit_next_year_rollover() -> None:
    assert event_content_reason("Анонс", "10 October", now=NOW) == "venue_event_expired"


def test_belgrade_local_date() -> None:
    now = datetime(2026, 10, 20, 23, tzinfo=UTC)
    assert event_content_reason("Анонс", "20.10.2026", now=now) == "venue_event_expired"


def test_naive_clock_refused() -> None:
    with pytest.raises(ValueError):
        event_content_reason("Анонс", "21.10.2026", now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(('text', 'expected'), [
    ('Концерт 31. 10. 2026. у 11', None),
    ('Концерт 10. 10. 2026. у 11', 'venue_event_expired'),
    ('Концерт 40. 10. 2026.', 'venue_event_date_missing'),
])
def test_spaced_numeric_dates(text, expected):
    assert event_content_reason(text, '', now=datetime(2026, 10, 21, tzinfo=UTC)) == expected
