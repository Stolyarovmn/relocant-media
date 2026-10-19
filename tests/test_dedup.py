from datetime import UTC, datetime, timedelta

from relocant_media.domain.dedup import (
    EventCandidate,
    canonicalize_url,
    cluster_events,
    content_sha256,
    same_event,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def candidate(
    item_id: int,
    source: str,
    *,
    url: str,
    title: str,
    body: str,
    published_at: datetime | None = NOW,
) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item_id,
        source_id=source,
        url=url,
        title=title,
        body=body,
        published_at=published_at,
    )


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    assert canonicalize_url(
        "HTTPS://Example.COM/news/?utm_source=tg&b=2&a=1#comments"
    ) == "https://example.com/news?a=1&b=2"


def test_exact_content_is_duplicate_even_when_urls_differ() -> None:
    left = candidate(1, "a", url="https://a.test/1", title="Новая линия", body="Текст")
    right = candidate(2, "a", url="https://a.test/2", title="Новая линия", body="Текст")
    assert content_sha256(left.title, left.body) == content_sha256(right.title, right.body)
    assert same_event(left, right)


def test_similar_titles_cluster_only_across_sources_and_inside_window() -> None:
    left = candidate(
        1,
        "city",
        url="https://city.test/1",
        title="Изменение движения трамваев на улице Краља Милана",
        body="Официальное сообщение",
    )
    other_source = candidate(
        2,
        "media",
        url="https://media.test/2",
        title="Изменение движения трамваев на улице Краља Милана сегодня",
        body="Пересказ сообщения",
        published_at=NOW + timedelta(hours=3),
    )
    same_source = candidate(
        3,
        "city",
        url="https://city.test/3",
        title="Изменение движения трамваев на улице Краља Милана сегодня",
        body="Другое сообщение",
        published_at=NOW + timedelta(hours=3),
    )
    late = candidate(
        4,
        "media-2",
        url="https://media.test/4",
        title="Изменение движения трамваев на улице Краља Милана сегодня",
        body="Позднее сообщение",
        published_at=NOW + timedelta(days=3),
    )

    assert same_event(left, other_source)
    assert not same_event(left, same_source)
    assert not same_event(left, late)


def test_cluster_events_handles_missing_dates_and_keeps_distinct_events() -> None:
    first = candidate(1, "a", url="https://a.test/1", title="Событие один", body="A")
    duplicate = candidate(
        2,
        "b",
        url="https://a.test/1?utm_source=x",
        title="Копия",
        body="B",
        published_at=NOW + timedelta(hours=1),
    )
    undated = candidate(
        3,
        "c",
        url="https://c.test/3",
        title="Другое событие",
        body="C",
        published_at=None,
    )

    clusters = cluster_events([undated, duplicate, first])
    assert [[item.raw_item_id for item in group] for group in clusters] == [[1, 2], [3]]
