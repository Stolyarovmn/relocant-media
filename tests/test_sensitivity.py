import pytest

from relocant_media.domain.policy import OWNER_DECISION_REASONS
from relocant_media.domain.sensitivity import (
    SENSITIVE_TOPIC_REASON,
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
