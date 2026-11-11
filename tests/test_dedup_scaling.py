"""Контроль объёма работы и эквивалентности группировки без хрупких таймеров."""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from relocant_media.domain import dedup

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def candidate(number: int, *, day: int = 0) -> dedup.EventCandidate:
    return dedup.EventCandidate(
        raw_item_id=number,
        source_id="city",
        url=f"https://city.test/{number}",
        title=f"Объявление городских служб {number}",
        body=f"Объект {number}. " + "Подробности городской программы и способы участия. " * 20,
        published_at=NOW + timedelta(days=day),
    )


def naive_clusters(items: list[dedup.EventCandidate]) -> list[list[int]]:
    ordered = sorted(
        items,
        key=lambda item: (
            item.published_at or datetime.max.replace(tzinfo=UTC),
            item.raw_item_id,
        ),
    )
    groups: list[list[dedup.EventCandidate]] = []
    for item in ordered:
        for group in groups:
            if any(dedup.same_event(item, member) for member in group):
                group.append(item)
                break
        else:
            groups.append([item])
    return [[item.raw_item_id for item in group] for group in groups]


def test_dense_same_source_parses_numeric_facts_once_per_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = dedup.numeric_facts
    calls = 0

    def counted(value: str, year: int) -> frozenset[str]:
        nonlocal calls
        calls += 1
        return original(value, year)

    monkeypatch.setattr(dedup, "numeric_facts", counted)
    items = [candidate(number) for number in range(180)]
    assert len(dedup.cluster_events(items)) == len(items)
    assert calls == len(items)


def test_history_outside_window_does_not_create_fuzzy_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = dedup.same_event
    calls = 0

    def counted(left: dedup.EventCandidate, right: dedup.EventCandidate) -> bool:
        nonlocal calls
        calls += 1
        return original(left, right)

    monkeypatch.setattr(dedup, "same_event", counted)
    items = [candidate(number, day=number * 3) for number in range(1000)]
    assert len(dedup.cluster_events(items)) == len(items)
    assert calls == 0


def test_index_preserves_earliest_cluster_exact_history_and_missing_dates() -> None:
    first, second = candidate(1), candidate(2)
    bridge = replace(
        candidate(3), canonical_url=second.url, content_sha256=first.resolved_content_sha256
    )
    old_exact = replace(candidate(4, day=10), canonical_url=second.url)
    undated_exact = replace(
        candidate(5), published_at=None, content_sha256=first.resolved_content_sha256
    )
    undated_distinct = replace(candidate(6), published_at=None)
    items = [undated_distinct, bridge, second, undated_exact, old_exact, first]
    result = [[item.raw_item_id for item in group] for group in dedup.cluster_events(items)]
    assert result == naive_clusters(items) == [[1, 3, 4, 5], [2], [6]]


def test_index_matches_naive_algorithm_on_dense_mixed_history() -> None:
    rng = random.Random(42711)
    items = [
        replace(candidate(number, day=rng.randrange(12)), source_id=f"source-{rng.randrange(4)}")
        for number in range(120)
    ]
    for number in range(120, 160):
        original = items[rng.randrange(120)]
        items.append(
            replace(
                original, raw_item_id=number, published_at=NOW + timedelta(days=rng.randrange(16))
            )
        )
    rng.shuffle(items)
    expected = naive_clusters(items)
    actual = [[item.raw_item_id for item in group] for group in dedup.cluster_events(items)]
    assert actual == expected


def test_replaced_candidate_does_not_inherit_cached_text_or_facts() -> None:
    first = replace(candidate(1), title="Работы во дворе", body="Объект 12. Работы начались.")
    second = replace(
        candidate(2), source_id="other", title=first.title, body="Объект 12. Работы продолжаются."
    )
    assert dedup.same_event(first, second)
    old_hash = second.resolved_content_sha256
    correction = replace(second, body=second.body.replace("12", "14"))
    assert correction.resolved_content_sha256 != old_hash
    assert not dedup.same_event(first, correction)
    assert second.resolved_content_sha256 == old_hash
