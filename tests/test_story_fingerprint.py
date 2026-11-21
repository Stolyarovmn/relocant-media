"""#333: one story from several sources must not become 2–3 feed posts."""
from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.domain.story_fingerprint import find_same_story, same_story

NOW = datetime(2026, 11, 17, 1, tzinfo=UTC)

# Обезличенные пары из ленты 29.10–17.11 (issue #333).
PAIRS = [
    (("«Црвена звезда» разгромила «Баварию»",
      "Баскетболисты «Црвены звезды» обыграли «Баварию» со счётом 103:73 в Евролиге."),
     ("Звезда победила Баварию 103:73",
      "В матче Евролиги «Црвена звезда» выиграла у «Баварии» 103:73. Следующая игра в Мадриде."),
     "sport"),
    (("Маршруты 26, 26N и 401N меняют путь на Дунайской",
      "С 31 октября автобусы 26, 26N и 401N пойдут в объезд Дунайской улицы."),
     ("Дунайская: объезд для линий 26, 401N и 26N",
      "31 октября линии 26, 26N и 401N объезжают участок Дунайской."),
     "transport"),
    (("Автобус №82 изменит маршрут до 11 декабря",
      "С 17 ноября по 11 декабря автобус №82 идёт в объезд из-за работ."),
     ("Линия 82: объезд с 17.11 по 11.12",
      "Маршрут 82 с 17.11 до 11.12 пойдёт по временной схеме."),
     "transport"),
]


@pytest.mark.parametrize(("first", "second", "kind"), PAIRS)
def test_second_retelling_is_the_same_story(first, second, kind):
    assert same_story(*second, *first) == kind


@pytest.mark.parametrize(("new", "old"), [
    # Другая линия в тот же день — отдельная новость.
    (("Автобус 83 в объезд 17 ноября", "Линия 83 17 ноября идёт в объезд."),
     ("Автобус 82 в объезд 17 ноября", "Линия 82 17 ноября идёт в объезд.")),
    # Та же линия, но новые даты — новая новость.
    (("Автобус 82 снова в объезд", "С 15 декабря автобус 82 идёт в объезд."),
     ("Автобус 82 в объезд", "С 17 ноября автобус 82 идёт в объезд.")),
    # Время концерта не счёт матча.
    (("Концерт в Коларце в 19:30", "Игра оркестра начнётся в 19:30, вход свободный."),
     ("Выставка в 19:30", "Открытие выставки в 19:30 в музее, матч тут ни при чём.")),
    # Одинаковый счёт у разных команд.
    (("«Партизан» обыграл «Олимпиакос» 2:1", "Матч завершился со счётом 2:1."),
     ("«Вождовац» обыграл «Раднички» 2:1", "Матч завершился со счётом 2:1.")),
])
def test_different_stories_stay_separate(new, old):
    assert same_story(*new, *old) is None


def test_window_limits_matches_to_recent_posts():
    first, second, _ = PAIRS[2]
    recent = [(7, *first, NOW - timedelta(hours=4))]
    assert find_same_story(*second, recent, now=NOW) == 7
    old = [(7, *first, NOW - timedelta(hours=80))]
    assert find_same_story(*second, old, now=NOW) is None
