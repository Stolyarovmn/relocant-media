from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.analytics.source_yield import build_source_yield_report
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source


def _source(source_id: str) -> Source:
    return Source(
        id=source_id,
        city="belgrade",
        name=source_id,
        url=f"https://example.test/{source_id}",
        source_type="official",
        trust=Decimal("1"),
        ownership="official",
        auto_publish=True,
    )


def _raw(source_id: str, external_id: str, hour: int) -> RawItem:
    return RawItem(
        source_id=source_id,
        external_id=external_id,
        url=f"https://example.test/{source_id}/{external_id}",
        title=f"Title {external_id}",
        body="Body",
        published_at=datetime(2026, 10, 21, hour, tzinfo=UTC),
        ingested_at=datetime(2026, 10, 21, hour, tzinfo=UTC),
    )


def test_source_yield_counts_public_and_staging_delivery_separately() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([_source("alpha"), _source("beta"), _source("zero")])
        session.flush()

        alpha_raw = [_raw("alpha", str(index), 10 + index) for index in range(1, 5)]
        beta_raw = _raw("beta", "1", 15)
        excluded = _raw("alpha", "outside", 9)
        session.add_all([*alpha_raw, beta_raw, excluded])
        session.flush()

        public_post = Post(
            city="belgrade",
            category="city",
            status="ready",
            title="Public",
            body="Body",
            confidence=Decimal("0.9"),
            telegram_message_id=101,
            published_at=datetime(2026, 10, 21, 12, 30, tzinfo=UTC),
        )
        staging_post = Post(
            city="belgrade",
            category="city",
            status="ready",
            title="Staging",
            body="Body",
            confidence=Decimal("0.9"),
        )
        review_post = Post(
            city="belgrade",
            category="city",
            status="human_review",
            title="Review",
            body="Body",
            confidence=Decimal("0.8"),
        )
        session.add_all([public_post, staging_post, review_post])
        session.flush()

        session.add_all(
            [
                EditorialRun(
                    raw_item_id=alpha_raw[1].id,
                    outcome={},
                    evidence={},
                    delivery_status="pending",
                ),
                EditorialRun(
                    raw_item_id=alpha_raw[2].id,
                    outcome={},
                    evidence={},
                    post_id=public_post.id,
                    delivery_status="sent",
                ),
                EditorialRun(
                    raw_item_id=alpha_raw[3].id,
                    outcome={},
                    evidence={},
                    post_id=staging_post.id,
                    delivery_status="sent",
                    staging_message_id=202,
                ),
                EditorialRun(
                    raw_item_id=beta_raw.id,
                    outcome={},
                    evidence={},
                    post_id=review_post.id,
                    delivery_status="pending",
                ),
            ]
        )
        session.flush()

        report = build_source_yield_report(
            session,
            city="belgrade",
            since=datetime(2026, 10, 21, 10, tzinfo=UTC),
            until=datetime(2026, 10, 21, 20, tzinfo=UTC),
        )

    engine.dispose()
    rows = {row["source_id"]: row for row in report["sources"]}
    assert rows["alpha"]["found"] == 4
    assert rows["alpha"]["editorial_runs"] == 3
    assert rows["alpha"]["post_created"] == 2
    assert rows["alpha"]["ready"] == 2
    assert rows["alpha"]["staging_sent"] == 1
    assert rows["alpha"]["public_sent"] == 1
    assert rows["alpha"]["rates"]["public_sent_from_found"] == 0.25

    assert rows["beta"]["found"] == 1
    assert rows["beta"]["editorial_runs"] == 1
    assert rows["beta"]["post_created"] == 1
    assert rows["beta"]["ready"] == 0
    assert rows["beta"]["public_sent"] == 0

    assert rows["zero"]["found"] == 0
    assert rows["zero"]["rates"]["reviewed_from_found"] is None
    assert report["totals"]["found"] == 5
    assert report["totals"]["public_sent"] == 1
    assert report["attribution"] == "anchor_source"


def test_source_yield_rejects_naive_or_reversed_windows() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="timezone-aware"):
            build_source_yield_report(
                session,
                city="belgrade",
                since=datetime(2026, 10, 21, 10),
                until=datetime(2026, 10, 21, 11, tzinfo=UTC),
            )
        with pytest.raises(ValueError, match="before until"):
            build_source_yield_report(
                session,
                city="belgrade",
                since=datetime(2026, 10, 21, 12, tzinfo=UTC),
                until=datetime(2026, 10, 21, 11, tzinfo=UTC),
            )
    engine.dispose()
