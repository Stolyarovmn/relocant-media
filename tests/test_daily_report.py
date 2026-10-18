import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from relocant_media.analytics.measurements import (
    record_ai_usage,
    record_channel_snapshot,
    record_post_snapshot,
    upsert_acquisition_cohort,
)
from relocant_media.analytics.report import build_daily_report, store_daily_report
from relocant_media.storage.models import DailyReport, ModerationAction, Post

RESET = text(
    "TRUNCATE daily_report, ai_usage_event, acquisition_cohort, channel_snapshot, "
    "post_snapshot, moderation_action, post RESTART IDENTITY CASCADE"
)


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    with Session(engine) as session:
        session.execute(RESET)
        session.commit()
        yield session
        session.rollback()
        session.execute(RESET)
        session.commit()
    engine.dispose()


def test_daily_report_uses_only_observed_metrics_and_keeps_currencies_separate(
    db_session: Session,
) -> None:
    post = Post(
        city="belgrade",
        category="utilities",
        status="published",
        title="Test",
        body="Test body",
        confidence=Decimal("0.9900"),
        published_at=datetime(2026, 10, 18, 8, tzinfo=UTC),
    )
    db_session.add(post)
    db_session.flush()
    record_post_snapshot(
        db_session,
        post_id=post.id,
        captured_at=datetime(2026, 10, 18, 9, tzinfo=UTC),
        horizon_hours=1,
        views=100,
        reactions=10,
        comments=2,
        forwards=3,
    )
    record_channel_snapshot(
        db_session,
        city="belgrade",
        captured_at=datetime(2026, 10, 18, 7, tzinfo=UTC),
        subscribers=1000,
        source="manual",
    )
    record_channel_snapshot(
        db_session,
        city="belgrade",
        captured_at=datetime(2026, 10, 18, 19, tzinfo=UTC),
        subscribers=1012,
        source="manual",
    )
    db_session.add(
        ModerationAction(
            telegram_chat_id=-1001,
            telegram_message_id=7,
            action="delete",
            reason="rules-v1",
        )
    )
    upsert_acquisition_cohort(
        db_session,
        city="belgrade",
        campaign="old-eur",
        cohort_date=date(2026, 10, 10),
        joins=20,
        spend=Decimal("100.00"),
        currency="EUR",
        retained_d1=15,
        retained_d7=10,
    )
    upsert_acquisition_cohort(
        db_session,
        city="belgrade",
        campaign="old-usd",
        cohort_date=date(2026, 10, 10),
        joins=10,
        spend=Decimal("40.00"),
        currency="USD",
        retained_d1=8,
        retained_d7=4,
    )
    record_ai_usage(
        db_session,
        city="belgrade",
        occurred_at=datetime(2026, 10, 18, 10, tzinfo=UTC),
        provider="google",
        model="gemini-3.8-flash",
        task="rank",
        status="ok",
        input_tokens=100,
        output_tokens=20,
        cost_amount=Decimal("0.010000"),
        currency="USD",
    )
    db_session.commit()

    payload = build_daily_report(
        db_session,
        report_date=date(2026, 10, 18),
        city="belgrade",
        timezone_name="Europe/Belgrade",
    )

    assert payload["content"]["published_posts"] == 1
    assert payload["content"]["engagement_per_view_observed"] == 0.15
    assert payload["audience"]["observed_delta"] == 12
    assert payload["moderation"]["by_action"] == {"delete": 1}
    assert payload["ai"]["costs_observed"] == {"USD": "0.010000"}
    assert payload["acquisition"]["retained_cpl_d7"]["EUR"]["cpl"] == "10.00"
    assert payload["acquisition"]["retained_cpl_d7"]["USD"]["cpl"] == "10.00"
    assert payload["limitations"][0].startswith("Metrics are observational")

    store_daily_report(
        db_session,
        report_date=date(2026, 10, 18),
        city="belgrade",
        payload=payload,
    )
    db_session.commit()
    stored = db_session.scalar(select(DailyReport))
    assert stored is not None
    assert stored.payload["audience"]["observed_delta"] == 12
