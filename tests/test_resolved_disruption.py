"""#320: закончившийся сбой через несколько часов не выходит как текущая новость."""
from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.domain.freshness import freshness_decision, resolved_disruption

NOW = datetime(2026, 11, 18, 22, 28, tzinfo=UTC)


@pytest.mark.parametrize(("title", "body"), [
    ("Uspostavljen tramvajski saobraćaj", "Tramvaji su zaustavljeni u 15 časova."),
    ("Izmene u saobraćaju zbog pljuska",
     "Tramvaji ponovo saobraćaju od 17 časova. Ranije su bili zaustavljeni."),
    ("Трамвайное движение возобновилось", "Его остановили в 15:00."),
    ("Вода в Земуне", "Подача воды восстановлена в 18:00. Работы завершены."),
    ("Otklonjen kvar na vodovodu u Borči", "Voda je vraćena."),
])
def test_resolved_disruption_is_detected(title, body):
    assert resolved_disruption(title, body)


@pytest.mark.parametrize(("title", "body"), [
    ("Isključenje vode u Zemunu sutra", "Bez vode od 9 do 14 časova."),
    ("Izmena trase linije 60", "Linija 60 od ponedeljka menja trasu. "
     "Saobraćaj na Brankovom mostu je ranije uspostavljen."),
    ("Концерт в Коларце", "Оркестр снова выступит в Белграде 30 ноября."),
])
def test_ongoing_or_future_notice_is_not_resolved(title, body):
    assert not resolved_disruption(title, body)


def test_old_resolved_notice_is_discarded_but_fresh_one_kept():
    title, body = "Tramvaji ponovo saobraćaju", "Saobraćaj je uspostavljen u 17 časova."
    old = freshness_decision(title, body, NOW - timedelta(hours=5), NOW,
                             published_at_precision="instant")
    assert (old.action, old.reason) == ("discard", "disruption_already_resolved")
    fresh = freshness_decision(title, body, NOW - timedelta(minutes=40), NOW,
                               published_at_precision="instant")
    assert fresh.action == "keep"
