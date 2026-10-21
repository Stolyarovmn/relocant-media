import pytest

from relocant_media.domain.draft_language import draft_language_reason


@pytest.mark.parametrize("body", [
    "Крајњи рок за завршетак изградње Новог српског моста је пролеће 2027. године.",
    "С 23 по 25 октября на теренима SC-a «Kovilovo» пройдёт такмичење у препонском јахању.",
    "Ulaz je besplatan, program počinje u deset časova.",
    "The event takes place in Belgrade and entry is free.",
    "Улаз бесплатан.",
])
def test_untranslated_body_is_blocked(body):
    assert draft_language_reason("[тестовый запуск] Новость Белграда", body)


@pytest.mark.parametrize("body", [
    "Часть Звездары останется без воды с 08:00 до 18:00.",
    "В Kovilovo пройдут соревнования «Trofej Beograda». Вход бесплатный.",
    "Работы на Egipatska и Vojvođanska продлены до 25 октября.",
    "Движение по улице «Карађорђева» изменится с 23 октября.",
])
def test_russian_prose_preserves_local_names(body):
    assert draft_language_reason("[тестовый запуск] Новости Белграда", body) is None


def test_non_russian_title_is_also_blocked():
    assert draft_language_reason("Saobraćaj u Beogradu", "Вход бесплатный.")
