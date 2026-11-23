import pytest

from relocant_media.domain.policy import OWNER_DECISION_REASONS
from relocant_media.domain.sensitivity import (
    PERSONAL_DETAIL_REASON,
    SENSITIVE_TOPIC_REASON,
    personal_detail_reason,
    sensitive_signals,
    sensitive_topic_reason,
)


@pytest.mark.parametrize(("text", "kind"), [
    ("Policija traga za 17-godišnjim dečakom koji boluje od autizma", "minor"),
    ("В Земуне ищут подростка, страдающего аутизмом", "health"),
    ("Пронађи ме: нестала девојчица из Борче", "missing"),
    ("Mladić nestao u Zemunu", "missing"),
    ("Nestala je žena iz Rakovice", "missing"),
    ("Мужчина погиб в ДТП на Газеле", "death"),
    ("Preminuo pešak udaren na pešačkom prelazu", "death"),
    ("Мужчину избили у кафе на Врачаре", "violence"),
    ("Ubistvo u Mirijevu", "violence"),
    ("Суицид в метро не подтверждён", "suicide"),
])
def test_detects_sensitive_substance(text: str, kind: str) -> None:
    assert kind in sensitive_signals([text])
    assert sensitive_topic_reason([text]) == SENSITIVE_TOPIC_REASON


@pytest.mark.parametrize("text", [
    "Nestanak struje u Zemunu od 9 do 14 časova",
    "Nestala struja na Novom Beogradu",
    "Завтра умеренный дождь",
    "Festival slavi 16 godina tradicije",
    "Predstava za decu u Domu omladine",
    "Нападающий «Партизана» забил два гола",
    "Концерт памяти композитора в Коларце",
    "Детский спектакль для 5-летних зрителей",
])
def test_ordinary_city_news_stays_clear(text: str) -> None:
    assert sensitive_signals([text]) == frozenset()


def test_sensitive_hold_goes_to_owner_decision() -> None:
    assert SENSITIVE_TOPIC_REASON in OWNER_DECISION_REASONS


SOURCE = ["Pronađi me: nestao 17-godišnji mladić iz Zemuna koji boluje od autizma"]


@pytest.mark.parametrize(("title", "body"), [
    ("Пропавший в Земуне подросток найден в автобусе",
     "Поиски 17-летнего юноши из Земуна, страдающего аутизмом, завершены."),
    ("Подросток из Земуна нашёлся", "Юношу с аутизмом нашли в автобусе. Розыск прекращён."),
])
def test_found_person_draft_with_diagnosis_is_held_beyond_owner_approval(title, body):
    assert personal_detail_reason(SOURCE, title, body) == PERSONAL_DETAIL_REASON
    assert PERSONAL_DETAIL_REASON not in OWNER_DECISION_REASONS


def test_found_person_short_draft_without_details_passes_this_check():
    assert personal_detail_reason(
        SOURCE, "Пропавший в Земуне подросток найден",
        "МВД сообщило, что поиск завершён: юношу нашли, он в безопасности.") is None


def test_active_search_stays_with_ordinary_owner_review():
    title, body = ("В Земуне ищут 17-летнего юношу",
                   "Полиция просит помочь: юноша с аутизмом пока не найден, приметы в карточке.")
    assert personal_detail_reason(SOURCE, title, body) is None
    assert sensitive_topic_reason([title, body]) == SENSITIVE_TOPIC_REASON


def test_minor_health_detail_outside_search_is_held():
    assert personal_detail_reason(
        ["Dečak povređen na pokretnim stepenicama"], "Мальчик пострадал на эскалаторе",
        "У 12-летнего мальчика с диагнозом эпилепсия травма руки.") == PERSONAL_DETAIL_REASON


@pytest.mark.parametrize(("sources", "title", "body"), [
    (["Dečja predstava u Čukarici"], "Детский спектакль в «Чукарице»",
     "14 ноября в 12:00 покажут спектакль для детей от 4 лет, вход 500 динаров."),
    (["Nestala struja u Zemunu"], "Свет в Земуне вернули",
     "Электричество восстановлено, неисправность найдена и устранена."),
])
def test_ordinary_news_not_held(sources, title, body):
    assert personal_detail_reason(sources, title, body) is None


def test_russian_search_wording_counts_as_active_search():
    assert personal_detail_reason(
        [], "Полиция ищет подростка", "Ищут 17-летнего подростка, страдающего аутизмом.") is None


OLD_NOTICE = "Pronađi me: nestao 15-godišnji dečak sa Vračara"
FOUND_NOTICE = "Pronađen maloletnik koji je nestao prošle nedelje"


def test_confirmed_outcome_in_source_wins_over_russian_draft_wording():
    assert personal_detail_reason(
        [FOUND_NOTICE], "Подросток вернулся к семье",
        "У 15-летнего подростка диагноз аутизм, родные благодарят полицию.",
    ) == PERSONAL_DETAIL_REASON


@pytest.mark.parametrize(("sources", "title", "body"), [
    ([], "Подростка доставили в больницу", "У подростка эпилепсия."),
    ([], "15-летний пассажир пострадал", "Ему назначили лекарства."),
    ([], "Полиция ищет свидетелей ДТП", "У школьника эпилепсия."),
    (["Povređen 15-godišnji putnik"], "В автобусе пострадал пассажир",
     "Пострадавшему назначили лекарства."),
    (["Povređen 15-godišnji putnik"], "В автобусе пострадал пассажир",
     "У него эпилепсия."),
    ([], "Объявлен розыск свидетелей ДТП", "У школьника эпилепсия."),
])
def test_individual_minor_health_without_diagnosis_label_is_held(sources, title, body):
    assert personal_detail_reason(sources, title, body) == PERSONAL_DETAIL_REASON


@pytest.mark.parametrize(("title", "body"), [
    ("Школьники изучают первую помощь", "На лекции рассказали об эпилепсии и лекарствах."),
    ("Клуб отмечает 15-летний юбилей", "В клубе собирают лекарства для больниц."),
    ("18-летний пассажир пострадал", "Ему назначили лекарства."),
])
def test_age_and_general_health_mentions_do_not_imply_a_specific_minor(title, body):
    assert personal_detail_reason([], title, body) is None


def test_private_case_in_source_does_not_block_general_health_draft():
    assert personal_detail_reason(
        ["Lekar je razgovarao sa 15-godišnjim putnikom."],
        "Врач проведёт школьную лекцию",
        "На лекции расскажут об эпилепсии и лекарствах.",
    ) is None


def test_old_search_notice_with_fresher_found_message_is_held():
    assert personal_detail_reason(
        [OLD_NOTICE, FOUND_NOTICE], "Пропавший на Врачаре подросток",
        "Подросток страдает аутизмом, его искали неделю.",
    ) == PERSONAL_DETAIL_REASON


def test_active_search_notice_alone_stays_with_ordinary_review():
    assert personal_detail_reason(
        [OLD_NOTICE], "На Врачаре разыскивают 15-летнего подростка",
        "Мальчик страдает аутизмом и может не отвечать на имя. Звоните 192.",
    ) is None


@pytest.mark.parametrize("body", [
    "У него эпилепсия, родным важно, чтобы он вовремя получил лекарства.",
    "Мальчик с особенностями развития вернулся домой.",
])
def test_found_person_health_detail_without_word_diagnosis_is_held(body):
    assert personal_detail_reason(
        [OLD_NOTICE, FOUND_NOTICE], "Подросток с Врачара нашёлся", body,
    ) == PERSONAL_DETAIL_REASON


def test_search_still_under_way_in_serbian_is_not_found():
    assert personal_detail_reason(
        ["Dečak sa Vračara još nije pronađen, potraga traje"], "Мальчика с Врачара всё ещё ищут",
        "15-летний мальчик страдает эпилепсией, ему нужны лекарства.",
    ) is None


@pytest.mark.parametrize(("sources", "title", "body"), [
    (["Sezona gripa u školama"], "В школах сезон гриппа",
     "У школьников растёт заболеваемость, врачи советуют прививки."),
    (["Kvar pronađen"], "Аптека снова работает",
     "Неисправность найдена, лекарства выдают по рецептам."),
])
def test_general_health_news_not_held(sources, title, body):
    assert personal_detail_reason(sources, title, body) is None
