from datetime import datetime, timedelta, timezone

from relocant_media.scheduler.models import SchedulerRun
from relocant_media.scheduler.state import DEFAULT_LEASE_DURATION, retry_is_due


def test_running_task_becomes_retryable_after_lease_window() -> None:
    started_at = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)
    run = SchedulerRun(
        task_key="source:belgrade:test",
        task_type="source_ingestion",
        city="belgrade",
        scheduled_for=started_at,
        attempt=1,
        status="running",
        started_at=started_at,
        details={},
    )

    assert not retry_is_due(run, started_at + DEFAULT_LEASE_DURATION - timedelta(seconds=1))
    assert retry_is_due(run, started_at + DEFAULT_LEASE_DURATION)
