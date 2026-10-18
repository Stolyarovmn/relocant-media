import os
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from relocant_media.analytics.measurements import (
    record_channel_snapshot,
    upsert_acquisition_cohort,
)
from relocant_media.storage.models import AcquisitionCohort, ChannelSnapshot

RESET = text(
    "TRUNCATE ai_usage_event, acquisition_cohort, channel_snapshot RESTART IDENTITY CASCADE"
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


def test_channel_snapshot_records_observation_and_correction(db_session: Session) -> None:
    record_channel_snapshot(
        db_session,
        city="belgrade",
        captured_at=datetime(2026, 10, 18, 8, tzinfo=UTC),
        subscribers=1000,
        source="manual",
    )
    record_channel_snapshot(
        db_session,
        city="belgrade",
        captured_at=datetime(2026, 10, 18, 9, tzinfo=UTC),
        subscribers=995,
        source="manual",
        kind="correction",
        note="Corrected operator entry",
    )
    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(ChannelSnapshot)) == 2


def test_acquisition_cohort_upsert_preserves_one_cohort(db_session: Session) -> None:
    common = {
        "city": "belgrade",
        "campaign": "test-campaign",
        "cohort_date": date(2026, 10, 1),
        "joins": 20,
        "spend": Decimal("100.00"),
        "currency": "eur",
    }
    upsert_acquisition_cohort(db_session, **common, retained_d1=15)
    upsert_acquisition_cohort(db_session, **common, retained_d1=14, retained_d7=10)
    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(AcquisitionCohort)) == 1
    cohort = db_session.scalar(select(AcquisitionCohort))
    assert cohort is not None
    assert cohort.currency == "EUR"
    assert cohort.retained_d1 == 14
    assert cohort.retained_d7 == 10


def test_acquisition_rejects_impossible_retention(db_session: Session) -> None:
    with pytest.raises(ValueError, match="retained_d7"):
        upsert_acquisition_cohort(
            db_session,
            city="belgrade",
            campaign="bad",
            cohort_date=date(2026, 10, 1),
            joins=5,
            retained_d7=6,
        )
