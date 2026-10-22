from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.analytics.category_learning import build_category_learning_report
from relocant_media.storage.models import (
    Base,
    EditorialRun,
    Post,
    PostSnapshot,
    RawItem,
    Source,
)


def _source(source_id: str) -> Source:
    return Source(
        id=source_id,
        city="belgrade",
        name=source_id,
        url=f"https://example.test/{source_id}",
        source_type="official",
        trust=Decimal("1"),
        ownership="official",
        auto_publish=False,
    )


def _add_public_post(
    session: Session,
    *,
    source_id: str,
    external_id: str,
    category: str,
    published_at: datetime,
    telegram_message_id: int,
    views: int,
    reactions: int,
) -> int:
    raw = RawItem(
        source_id=source_id,
        external_id=external_id,
        url=f"https://example.test/{source_id}/{external_id}",
        title=f"Raw {external_id}",
        body="Body",
        published_at=published_at,
        ingested_at=published_at,
    )
    session.add(raw)
    session.flush()
    post = Post(
        city="belgrade",
        category=category,
        status="ready",
        title=f"Post {external_id}",
        body="Body",
        confidence=Decimal("0.9"),
        telegram_message_id=telegram_message_id,
        published_at=published_at,
    )
    session.add(post)
    session.flush()
    session.add(
        EditorialRun(
            raw_item_id=raw.id,
            outcome={},
            evidence={},
            post_id=post.id,
            delivery_status="sent",
        )
    )
    session.add(
        PostSnapshot(
            post_id=post.id,
            captured_at=published_at + timedelta(hours=24),
            horizon_hours=24,
            views=views,
            reactions=reactions,
            comments=0,
            forwards=0,
        )
    )
    return post.id


def test_category_learning_uses_latest_fixed_horizon_snapshot_and_anchor_source() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([_source("city_source"), _source("transport_source")])
        session.flush()

        first_city_id = _add_public_post(
            session,
            source_id="city_source",
            external_id="city-1",
            category="city",
            published_at=start + timedelta(days=1),
            telegram_message_id=101,
            views=80,
            reactions=8,
        )
        session.add(
            PostSnapshot(
                post_id=first_city_id,
                captured_at=start + timedelta(days=2, minutes=5),
                horizon_hours=24,
                views=100,
                reactions=10,
                comments=0,
                forwards=0,
            )
        )
        _add_public_post(
            session,
            source_id="city_source",
            external_id="city-2",
            category="city",
            published_at=start + timedelta(days=2),
            telegram_message_id=102,
            views=120,
            reactions=10,
        )
        _add_public_post(
            session,
            source_id="transport_source",
            external_id="transport-1",
            category="transport",
            published_at=start + timedelta(days=3),
            telegram_message_id=201,
            views=40,
            reactions=1,
        )
        _add_public_post(
            session,
            source_id="transport_source",
            external_id="transport-2",
            category="transport",
            published_at=start + timedelta(days=4),
            telegram_message_id=202,
            views=60,
            reactions=1,
        )
        session.flush()

        report = build_category_learning_report(
            session,
            city="belgrade",
            since=start,
            until=start + timedelta(days=10),
            horizon_hours=24,
            minimum_samples=2,
        )

    engine.dispose()
    rows = {row["category"]: row for row in report["categories"]}
    assert rows["city"]["observed_posts"] == 2
    assert rows["city"]["views"] == 220
    assert rows["city"]["views_per_observed_post"] == 110.0
    assert rows["city"]["anchor_sources"] == {"city_source": 2}
    assert rows["transport"]["views_per_observed_post"] == 50.0
    assert rows["transport"]["anchor_sources"] == {"transport_source": 2}

    recommendations = {row["category"]: row for row in report["recommendations"]}
    assert recommendations["city"]["code"] == "review_discovery_expansion"
    assert recommendations["transport"]["code"] == "review_discovery_efficiency"
    assert all(row["class"] == "PR_REVIEW" for row in recommendations.values())
    assert report["automatic_adaptation"] is False
    assert report["editorial_hard_gates_unchanged"] is True
    assert report["causal_growth_claim"] is False


def test_category_learning_ignores_non_sent_posts_and_missing_snapshots() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with Session(engine) as session:
        session.add(_source("source"))
        session.flush()
        raw = RawItem(
            source_id="source",
            external_id="pending",
            url="https://example.test/source/pending",
            title="Pending",
            body="Body",
            published_at=start + timedelta(days=1),
            ingested_at=start + timedelta(days=1),
        )
        session.add(raw)
        session.flush()
        post = Post(
            city="belgrade",
            category="city",
            status="ready",
            title="Pending",
            body="Body",
            confidence=Decimal("0.9"),
            telegram_message_id=999,
            published_at=start + timedelta(days=1),
        )
        session.add(post)
        session.flush()
        session.add(
            EditorialRun(
                raw_item_id=raw.id,
                outcome={},
                evidence={},
                post_id=post.id,
                delivery_status="pending",
            )
        )
        session.add(
            PostSnapshot(
                post_id=post.id,
                captured_at=start + timedelta(days=2),
                horizon_hours=24,
                views=9999,
                reactions=999,
                comments=0,
                forwards=0,
            )
        )
        session.flush()

        report = build_category_learning_report(
            session,
            city="belgrade",
            since=start,
            until=start + timedelta(days=10),
            minimum_samples=2,
        )

    engine.dispose()
    row = report["categories"][0]
    assert row["published_posts"] == 1
    assert row["public_sent"] == 0
    assert row["observed_posts"] == 0
    assert row["views"] == 0
    assert row["snapshot_coverage"] is None
    assert report["recommendations"][0]["code"] == "collect_fixed_horizon_samples"


def test_category_learning_rejects_invalid_windows_and_sampling_parameters() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="timezone-aware"):
            build_category_learning_report(
                session,
                city="belgrade",
                since=datetime(2026, 10, 1),
                until=datetime(2026, 10, 2, tzinfo=UTC),
            )
        with pytest.raises(ValueError, match="before until"):
            build_category_learning_report(
                session,
                city="belgrade",
                since=datetime(2026, 10, 2, tzinfo=UTC),
                until=datetime(2026, 10, 1, tzinfo=UTC),
            )
        with pytest.raises(ValueError, match="positive"):
            build_category_learning_report(
                session,
                city="belgrade",
                since=datetime(2026, 10, 1, tzinfo=UTC),
                until=datetime(2026, 10, 2, tzinfo=UTC),
                horizon_hours=0,
            )
        with pytest.raises(ValueError, match="at least 2"):
            build_category_learning_report(
                session,
                city="belgrade",
                since=datetime(2026, 10, 1, tzinfo=UTC),
                until=datetime(2026, 10, 2, tzinfo=UTC),
                minimum_samples=1,
            )
    engine.dispose()
