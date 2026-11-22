from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.domain.flight_notices import find_related_flight_cancellation
from relocant_media.domain.story_fingerprint import same_story

NOW = datetime(2026, 11, 22, 17, 10, tzinfo=UTC)
# Публичные заголовки 153/154/155: три источника, один перевозчик/маршрут/день.
POSTS = [
    (153, "Er Srbija отменила рейс из Белграда в Брюссель на 23 ноября",
     "Пассажирам с бронированиями на 23 ноября сообщат об альтернативных вариантах.",
     NOW - timedelta(hours=3)),
    (154, "Эр Сербия отменила рейс из Белграда в Брюссель из-за забастовки",
     "Если у вас куплен билет на понедельник, обратите внимание: рейс отменён.",
     NOW - timedelta(hours=1)),
    (155, "Air Serbia отменила рейсы между Белградом и Брюсселем на 23 ноября",
     "Все рейсы на понедельник, 23 ноября отменены. Контактный центр: 011 311 2123.",
     NOW - timedelta(minutes=8)),
]


@pytest.mark.parametrize("index", [1, 2])
def test_real_retellings_need_linked_update_despite_different_wording(index):
    _, title, body, _ = POSTS[index]
    assert same_story(title, body, POSTS[0][1], POSTS[0][2]) is None
    assert find_related_flight_cancellation(title, body, [POSTS[0]], now=NOW) == 153


@pytest.mark.parametrize("before,after", [
    ("23 ноября", "24 ноября"),
    ("23 ноября", "23 ноября 2027"),
    ("Брюсселем", "Парижем"),
    ("Белградом", "Берлином"),
    ("Air Serbia", "Lufthansa"),
    ("отменила", "возобновила"),
    ("отменила", "задержала"),
    ("отменила", "не отменила"),
])
def test_other_date_route_carrier_or_status_is_not_a_match(before, after):
    _, title, body, _ = POSTS[2]
    title, body = title.replace(before, after), body.replace(before, after)
    assert find_related_flight_cancellation(title, body, [POSTS[0]], now=NOW) is None


def test_unknown_date_multi_date_and_recurring_wording_are_not_assumed_equal():
    _, title, _, _ = POSTS[1]
    for body in ["Дата неизвестна.", "Отмена 23 ноября и 24 ноября.",
                 "Рейсы отменены каждый раз в понедельник."]:
        assert find_related_flight_cancellation(title, body, [POSTS[0]], now=NOW) is None


def test_history_time_is_required_and_future_or_old_history_is_ignored():
    post_id, title, body, _ = POSTS[0]
    for published in [None, NOW + timedelta(minutes=1), NOW - timedelta(hours=73)]:
        assert find_related_flight_cancellation(
            title, body, [(post_id, title, body, published)], now=NOW) is None


def test_changed_scope_is_held_for_review_instead_of_silently_called_duplicate():
    _, title, body, _ = POSTS[2]
    assert find_related_flight_cancellation(
        title, body + " Дополнительные рейсы JU123 также отменены.", [POSTS[0]], now=NOW) == 153
