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
    "Движение по улице Карагеоргиевой (Karađorđeva) изменится с 23 октября.",
    "Ремонт на Воеводинской (Vojvođanska) продлится до 31 декабря.",
])
def test_russian_prose_preserves_local_names(body):
    assert draft_language_reason("[тестовый запуск] Новости Белграда", body) is None


def test_non_russian_title_is_also_blocked():
    assert draft_language_reason("Saobraćaj u Beogradu", "Вход бесплатный.")


# #316: заглавная буква не делает сербскую кириллицу допустимой в русском тексте.
@pytest.mark.parametrize("body", [
    "На Војвођанской между Хајдук Вељкова и Десанке Максимовић открыто по полосе.",
    "Подтопления затронули Южный бульвар, Војвођанска улица на Новом Белграде.",
    "Земунски пут закрыт у Сајмиште и Зарије Вујошевића.",
    "Движение по улице «Карађорђева» изменится с 23 октября.",
])
def test_serbian_cyrillic_names_are_blocked(body):
    assert draft_language_reason("Новости Белграда", body)


def test_serbian_cyrillic_in_title_is_blocked():
    assert draft_language_reason("Перекрытие на Војвођанской", "Вход бесплатный.")


def test_transport_brief_rejects_serbian_cyrillic():
    from pydantic import ValidationError

    from relocant_media.ops.belgrade_digests import TransportBrief

    with pytest.raises(ValidationError, match="Serbian Cyrillic"):
        TransportBrief(text="Земунски пут закрыт у Сајмиште.", details=())
    assert TransportBrief(text="Земунский путь (Zemunski put) закрыт.", details=()).text
