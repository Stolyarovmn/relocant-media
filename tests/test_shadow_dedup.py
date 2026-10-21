from datetime import UTC, datetime, timedelta

from relocant_media.domain.dedup import EventCandidate, same_event
from relocant_media.domain.shadow_dedup import (
    ShadowDuplicateLabel,
    evaluate_shadow_labels,
    find_shadow_duplicates,
)


def _candidate(
    raw_id: int,
    source: str,
    title: str,
    body: str,
    when: datetime,
    *,
    url: str | None = None,
) -> EventCandidate:
    return EventCandidate(
        raw_item_id=raw_id,
        source_id=source,
        url=url or f"https://{source}.example/{raw_id}",
        title=title,
        body=body,
        published_at=when,
    )


def test_cross_script_duplicate_is_shadow_signal_but_not_authoritative_duplicate() -> None:
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    left = _candidate(
        1,
        "city",
        "Затварање улице Кнеза Милоша у суботу",
        "Улица Кнеза Милоша биће затворена 24. октобра од 8 до 18 часова.",
        now,
    )
    right = _candidate(
        2,
        "transport",
        "Zatvaranje ulice Kneza Milosa u subotu",
        "Ulica Kneza Milosa bice zatvorena 24. oktobra od 8 do 18 casova.",
        now + timedelta(minutes=20),
    )

    assert same_event(left, right) is False
    signals = find_shadow_duplicates([left, right])

    assert len(signals) == 1
    assert signals[0].possible_duplicate is True
    assert signals[0].score.score >= signals[0].threshold
    assert signals[0].score.title_overlap == 1.0


def test_shadow_does_not_repeat_authoritative_duplicate_or_compare_outside_window() -> None:
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    exact_left = _candidate(1, "city", "Same", "Same body", now, url="https://x.example/a")
    exact_right = _candidate(
        2,
        "other",
        "Same",
        "Same body",
        now + timedelta(minutes=5),
        url="https://x.example/a",
    )
    old = _candidate(
        3,
        "archive",
        "Затварање улице Кнеза Милоша у суботу",
        "Улица Кнеза Милоша биће затворена 24. октобра од 8 до 18 часова.",
        now - timedelta(days=5),
    )
    recent = _candidate(
        4,
        "transport",
        "Zatvaranje ulice Kneza Milosa u subotu",
        "Ulica Kneza Milosa bice zatvorena 24. oktobra od 8 do 18 casova.",
        now,
    )

    assert find_shadow_duplicates([exact_left, exact_right, old, recent]) == []


def test_near_threshold_title_miss_can_be_observed_without_changing_same_event() -> None:
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    body = "Linija 26 privremeno saobraca izmenjenom trasom od 22. do 24. oktobra zbog radova."
    left = _candidate(
        10,
        "city",
        "Linija 26 menja trasu zbog radova u centru",
        body,
        now,
    )
    right = _candidate(
        11,
        "transport",
        "Linija 26 menja smer zbog radova u centru",
        body,
        now + timedelta(minutes=10),
    )

    assert same_event(left, right) is False
    signals = find_shadow_duplicates([left, right])

    assert len(signals) == 1
    assert signals[0].score.body_overlap == 1.0
    assert signals[0].score.anchor_overlap == 1.0


def test_reviewed_labels_expose_false_positive_and_false_negative_rates() -> None:
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    body = "Linija 26 privremeno saobraca izmenjenom trasom od 22. do 24. oktobra zbog radova."
    candidates = [
        _candidate(1, "a", "Linija 26 menja trasu zbog radova u centru", body, now),
        _candidate(
            2,
            "b",
            "Linija 26 menja smer zbog radova u centru",
            body,
            now + timedelta(minutes=5),
        ),
        _candidate(3, "c", "Potpuno druga vest o muzeju", "Otvaranje izlozbe.", now),
    ]
    signals = find_shadow_duplicates(candidates)
    metrics = evaluate_shadow_labels(
        signals,
        [
            ShadowDuplicateLabel(1, 2, True),
            ShadowDuplicateLabel(1, 3, False),
            ShadowDuplicateLabel(2, 3, True),
        ],
    )

    assert metrics["true_positive"] == 1
    assert metrics["false_positive"] == 0
    assert metrics["false_negative"] == 1
    assert metrics["true_negative"] == 1
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 0.5
    assert metrics["false_positive_rate"] == 0.0
    assert metrics["false_negative_rate"] == 0.5
