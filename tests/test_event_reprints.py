"""Синтетические примеры по типам повторов54/55,78/83 и расхождению82/84.

Тексты написаны для теста; чужие статьи и публикации здесь не сохраняются.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.domain.dedup import (
    EventCandidate,
    content_sha256,
    event_key,
    find_repeated_publication,
    same_event,
)
from relocant_media.domain.event_facts import event_dates
from relocant_media.services.editorial import _reviewed_event_item
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source

NOW = datetime(2026, 11, 6, 10, tzinfo=UTC)


def item(number: int, body: str, *, source: str = "city") -> EventCandidate:
    return EventCandidate(
        number, source, f"https://city.example/{number}", f"Объявление {number}", body, NOW
    )


def shortened() -> tuple[EventCandidate, EventCandidate]:
    return (
        item(
            1,
            "Radovi u Javorskoj ulici od 1. do 2. novembra. "
            "Linije 12, 12N i 405N voze od Lipove ulice do SRC-a „Ana Mali Park”.",
        ),
        item(
            2,
            "U ulici Javorska radovi od 01. do 02. novembra 2026. godine. "
            "Linije broj 12, 12N i 405N voze od ulice Lipova "
            "do terminusa „Centar /SRC Ana Mali Park/”.",
        ),
    )


def enhanced() -> tuple[EventCandidate, EventCandidate]:
    return (
        item(
            3,
            "Pojačan prevoz 7. novembra od 8 do 16 časova. "
            "Linije 12, 25, 32, 35 i 50. Privremeno se uspostavlja linija "
            "75L (GO Novi Grad – Gradsko groblje) sa dva vozila.",
        ),
        item(
            4,
            "Povećan broj vozila 07. novembra 2026. godine od 8.00 do 16.00 časova. "
            "Ka grobljima: 12, 25, 32, 35 i 50. Uspostavlja se linija "
            "75L (GO Novi Grad – Gradsko groblje) sa dva vozila.",
        ),
    )


@pytest.mark.parametrize("factory", [shortened, enhanced])
def test_anchored_service_reprints_without_stop_ids(factory):
    left, right = factory()
    assert same_event(left, right)
    assert same_event(right, left)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("02. novembra", "03. novembra"),
        ("2026", "2027"),
        ("novembra", "decembra"),
        ("405N", "406N"),
        ("Lipova", "Brezova"),
        ("Ana Mali Park", "Ana Novi Park"),
        ("voze", "ne voze"),
    ],
)
def test_shortened_changes_remain_new(before: str, after: str) -> None:
    left, right = shortened()
    assert not same_event(left, replace(right, body=right.body.replace(before, after)))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("8.00", "9.00"),
        ("75L", "76L"),
        ("35", "36"),
        ("GO Novi Grad – Gradsko groblje", "Gradsko groblje – GO Novi Grad"),
        ("Uspostavlja se", "Ne uspostavlja se"),
    ],
)
def test_enhanced_changes_remain_new(before: str, after: str) -> None:
    left, right = enhanced()
    assert not same_event(left, replace(right, body=right.body.replace(before, after)))


def test_source_revision_does_not_return_to_reviewed_source() -> None:
    left, right = shortened()
    revision = replace(right, canonical_url=right.url + "?__source_revision=changed")
    assert not same_event(left, revision)
    assert event_key(left) != event_key(revision)


def test_street_order_is_not_erased() -> None:
    left, right = shortened()
    body = right.body.replace("Javorska", "TEMP").replace("Lipova", "Javorska")
    assert not same_event(left, replace(right, body=body.replace("TEMP", "Lipova")))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("8 до 12", "9 до 12"),
        ("2026", "2027"),
        ("ноября", "декабря"),
        ("Берлине", "Праге"),
        ("12", "14"),
        ("отменены", "восстановлены"),
    ],
)
def test_identical_headlines_do_not_hide_material_changes(before: str, after: str) -> None:
    body = "В Берлине 8 ноября 2026 года с 8 до 12 часов отменены маршруты 12 и 26."
    left = replace(item(1, body), title="Временные изменения движения городского транспорта")
    right = replace(item(2, body.replace(before, after), source="other"), title=left.title)
    assert not same_event(left, right)


def test_dates_keep_month_year_and_normalize_leading_zeroes() -> None:
    assert event_dates("od 01. do 02. novembra 2026.", 2026) == {
        "2026-11-01",
        "2026-11-02",
    }


@pytest.mark.parametrize("delivery_status", ["sent", "uncertain"])
def test_actual_reviewed_filter_keeps_receipts_and_hashes(delivery_status: str) -> None:
    left, right = shortened()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = Source(
            id="city",
            city="test-city",
            name="Official",
            url=left.url,
            source_type="official",
            trust=Decimal("1"),
            enabled=True,
        )
        session.add(source)
        session.flush()
        items = []
        for candidate in (left, right):
            raw = RawItem(
                source_id=source.id,
                external_id=str(candidate.raw_item_id),
                url=candidate.url,
                title=candidate.title,
                body=candidate.body,
                canonical_url=candidate.url,
                content_sha256=content_sha256(candidate.title, candidate.body),
                published_at=candidate.published_at,
            )
            session.add(raw)
            items.append(raw)
        session.flush()
        run = EditorialRun(
            raw_item_id=items[0].id,
            outcome={"status": "ready"},
            evidence={"receipt": "historical"},
            delivery_status=delivery_status,
            staging_message_id=12345,
        )
        session.add(run)
        session.flush()
        old_hash = items[0].content_sha256
        reviewed = _reviewed_event_item(session, anchor=items[1], anchor_source=source)
        assert reviewed is not None and reviewed.id == items[0].id
        items[1].body = items[1].body.replace("02. novembra", "03. novembra")
        items[1].content_sha256 = content_sha256(items[1].title, items[1].body)
        session.flush()
        assert _reviewed_event_item(session, anchor=items[1], anchor_source=source) is None
        assert (run.delivery_status, run.staging_message_id, run.evidence) == (
            delivery_status,
            12345,
            {"receipt": "historical"},
        )
        assert items[0].content_sha256 == old_hash


def test_repeated_draft_uses_body_not_just_score() -> None:
    previous = "Клуб выиграл со счётом 103:73. Болельщикам рассказали о следующей встрече."
    assert find_repeated_publication("Итоги матча", previous, [(45, "Победа", previous)]) == 45
    assert (
        find_repeated_publication(
            "Тренер о матче",
            "Тренер объяснил смену состава. Счёт103:73.",
            [(45, "Победа", previous)],
        )
        is None
    )
    assert find_repeated_publication("Матч отменён", previous, [(45, "Победа", previous)]) is None


def test_near_identical_draft_preserves_new_numeric_fact() -> None:
    body = (
        "В Берлине состоится встреча участников городского проекта. "
        "На площадке организаторы расскажут о планах и ответят на вопросы жителей. "
        "Начало встречи назначено на 18:00. Приходите заранее."
    )
    old = [(88, "Встреча жителей", body)]
    assert (
        find_repeated_publication(
            "Встреча жителей", body.replace("заранее", "заранее заранее"), old
        )
        == 88
    )
    assert find_repeated_publication("Встреча жителей", body.replace("18:00", "19:00"), old) is None


def test_publication_formatting_does_not_hide_repeated_content() -> None:
    body = "Встреча жителей состоится в 18:00. Организаторы ответят на вопросы."
    decorated = "Рубрика: Город\n" + body + "\nКарта из источника: https://maps.example/123"
    assert (
        find_repeated_publication("Встреча", body, [(7, "[тестовый запуск] Встреча", decorated)])
        == 7
    )
    assert (
        find_repeated_publication("[тестовый запуск] Встреча", decorated, [(7, "Встреча", body)])
        == 7
    )
    assert (
        find_repeated_publication(
            "Встреча", body.replace("18:00", "19:00"), [(7, "Встреча", decorated)]
        )
        is None
    )


def test_reversed_score_is_a_changed_fact_even_in_long_identical_draft() -> None:
    body = "Счёт матча 103:73. " + "Команда провела встречу на домашней арене. " * 6
    assert (
        find_repeated_publication(
            "Результат", body.replace("103:73", "73:103"), [(45, "Результат", body)]
        )
        is None
    )


def test_unrelated_headlines_skip_full_text_fact_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    left = replace(item(1, "Большой исходный текст. " * 500), title="Транспорт меняет маршрут")
    right = replace(
        item(2, "Другая длинная статья. " * 500, source="other"),
        title="Музей приглашает на выставку",
    )

    def forbidden(*args: object, **kwargs: object) -> bool:
        raise AssertionError("Нельзя разбирать факты у заведомо разных заголовков")

    monkeypatch.setattr("relocant_media.domain.dedup.EventCandidate._facts", forbidden)
    assert not same_event(left, right)
    assert not same_event(right, left)
