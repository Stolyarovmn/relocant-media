import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from relocant_media.scheduler.config import CityOperationsConfig
from relocant_media.scheduler.metrics import (
    ChannelMetrics,
    PostMetrics,
    UnavailablePostMetricsProvider,
)
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.scheduler.runner import (
    SchedulerSummary,
    run_scheduler_tick,
    schedule_channel_snapshot,
    schedule_daily_report,
    schedule_post_snapshots,
)
from relocant_media.scheduler.state import claim_lease, release_lease
from relocant_media.scheduler.timing import parse_interval, retry_backoff
from relocant_media.storage.models import (
    ChannelSnapshot,
    DailyReport,
    Post,
    PostSnapshot,
    RawItem,
)

FIXTURES = Path(__file__).parent / "fixtures"
RESET_TABLES = text(
    "TRUNCATE scheduler_run, scheduler_lease, channel_snapshot, post_snapshot, post, daily_report, "
    "collector_failure, raw_item, source RESTART IDENTITY CASCADE"
)


def _test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(_test_database_url(), pool_pre_ping=True)
    with Session(engine) as session:
        session.execute(RESET_TABLES)
        session.commit()
        yield session
        session.rollback()
        session.execute(RESET_TABLES)
        session.commit()
    engine.dispose()


def test_interval_and_retry_parsing() -> None:
    assert parse_interval("10m") == timedelta(minutes=10)
    assert parse_interval("4h") == timedelta(hours=4)
    assert parse_interval("2d") == timedelta(days=2)
    assert retry_backoff(1) == timedelta(minutes=1)
    assert retry_backoff(7) == timedelta(minutes=60)
    with pytest.raises(ValueError):
        parse_interval("10s")


def test_lease_prevents_overlap_and_expires(db_session: Session) -> None:
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    assert claim_lease(db_session, task_key="source:belgrade:test", now=now)
    assert not claim_lease(db_session, task_key="source:belgrade:test", now=now)
    assert claim_lease(
        db_session,
        task_key="source:belgrade:test",
        now=now + timedelta(minutes=16),
    )
    release_lease(
        db_session,
        task_key="source:belgrade:test",
        now=now + timedelta(minutes=16),
    )


def test_source_schedule_is_idempotent_within_interval(
    db_session: Session,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "news.yaml").write_text(
        """id: scheduled_city_news
name: Scheduled City News
publisher: Test City
ownership: official
url: https://example.test/lat/vesti
language: sr-Latn
source_type: official_city_news
trust: 1.0
check_every: 10m
categories: [city]
extraction:
  strategy: beograd_news_list
  status: active
verification:
  checked_at: 2026-10-18
""",
        encoding="utf-8",
    )
    html = (FIXTURES / "beograd_news_list.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    config = CityOperationsConfig(id="belgrade", timezone="Europe/Belgrade")
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)

    async def run_twice() -> tuple[SchedulerSummary, SchedulerSummary]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await run_scheduler_tick(
                db_session,
                client,
                city_config=config,
                source_directory=source_dir,
                now=now,
                metrics_provider=UnavailablePostMetricsProvider(),
            )
            second = await run_scheduler_tick(
                db_session,
                client,
                city_config=config,
                source_directory=source_dir,
                now=now,
                metrics_provider=UnavailablePostMetricsProvider(),
            )
        return first, second

    first, second = asyncio.run(run_twice())
    assert first.source_runs == 1
    assert second.source_runs == 0
    assert db_session.scalar(select(func.count()).select_from(RawItem)) == 2
    source_runs = db_session.scalar(
        select(func.count())
        .select_from(SchedulerRun)
        .where(SchedulerRun.task_type == "source_ingestion")
    )
    assert source_runs == 1


class FakeMetricsProvider:
    async def fetch_post_metrics(self, post: Post) -> PostMetrics:
        assert post.telegram_message_id == 1234
        return PostMetrics(views=100, reactions=10, comments=4, forwards=3)

    async def fetch_channel_metrics(self) -> ChannelMetrics:
        return ChannelMetrics(
            subscribers=12345,
            source="test:channel_metrics",
        )


def _published_post(session: Session, now: datetime) -> Post:
    post = Post(
        city="belgrade",
        category="transport",
        status="published",
        title="Test",
        body="Test body",
        confidence=Decimal("0.9500"),
        telegram_message_id=1234,
        published_at=now - timedelta(hours=2),
    )
    session.add(post)
    session.commit()
    return post


def test_due_channel_snapshot_records_only_observed_metrics(db_session: Session) -> None:
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    config = CityOperationsConfig(id="belgrade", timezone="Europe/Belgrade")
    summary = SchedulerSummary()

    asyncio.run(
        schedule_channel_snapshot(
            db_session,
            city_config=config,
            now=now,
            provider=FakeMetricsProvider(),
            summary=summary,
        )
    )

    assert summary.channel_snapshots_recorded == 1
    snapshot = db_session.scalar(select(ChannelSnapshot).where(ChannelSnapshot.city == "belgrade"))
    assert snapshot is not None
    assert snapshot.subscribers == 12345
    assert snapshot.source == "test:channel_metrics"


def test_due_post_snapshot_records_only_observed_metrics(db_session: Session) -> None:
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    post = _published_post(db_session, now)
    summary = SchedulerSummary()

    asyncio.run(
        schedule_post_snapshots(
            db_session,
            city="belgrade",
            now=now,
            provider=FakeMetricsProvider(),
            summary=summary,
        )
    )

    assert summary.snapshots_recorded == 1
    snapshot = db_session.scalar(select(PostSnapshot).where(PostSnapshot.post_id == post.id))
    assert snapshot is not None
    assert snapshot.horizon_hours == 1
    assert snapshot.views == 100
    assert snapshot.reactions == 10
    assert snapshot.comments == 4
    assert snapshot.forwards == 3


def test_missing_metrics_provider_is_blocked_with_backoff(db_session: Session) -> None:
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    post = _published_post(db_session, now)
    first = SchedulerSummary()
    second = SchedulerSummary()

    asyncio.run(
        schedule_post_snapshots(
            db_session,
            city="belgrade",
            now=now,
            provider=UnavailablePostMetricsProvider(),
            summary=first,
        )
    )
    asyncio.run(
        schedule_post_snapshots(
            db_session,
            city="belgrade",
            now=now,
            provider=UnavailablePostMetricsProvider(),
            summary=second,
        )
    )

    assert first.snapshots_blocked == 1
    assert second.snapshots_blocked == 0
    assert db_session.scalar(select(PostSnapshot).where(PostSnapshot.post_id == post.id)) is None
    run = db_session.scalar(
        select(SchedulerRun).where(SchedulerRun.task_key == f"snapshot:belgrade:{post.id}:1h")
    )
    assert run is not None
    assert run.status == "blocked"
    assert run.error_type == "MetricsProviderUnavailable"


def test_daily_report_is_created_once_for_local_day(db_session: Session) -> None:
    config = CityOperationsConfig(id="belgrade", timezone="Europe/Belgrade")
    now = datetime(2026, 10, 18, 22, 20, tzinfo=UTC)
    first = SchedulerSummary()
    second = SchedulerSummary()

    schedule_daily_report(db_session, city_config=config, now=now, summary=first)
    schedule_daily_report(db_session, city_config=config, now=now, summary=second)

    assert first.reports_stored == 1
    assert second.reports_stored == 0
    report = db_session.scalar(select(DailyReport).where(DailyReport.city == "belgrade"))
    assert report is not None
    assert report.report_date.isoformat() == "2026-10-18"
