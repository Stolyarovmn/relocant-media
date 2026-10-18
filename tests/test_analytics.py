from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from relocant_media.services.analytics import (
    DEFAULT_POST_SNAPSHOT_HOURS,
    PostSnapshot,
    effective_cpl,
    engagement_per_view,
    planned_snapshot_times,
    retained_cpl,
)


def test_engagement_per_view() -> None:
    snapshot = PostSnapshot(views=100, reactions=10, comments=4, forwards=6)
    assert engagement_per_view(snapshot) == 0.2


def test_effective_cpl() -> None:
    assert effective_cpl(50.0, 10) == 5.0
    assert effective_cpl(50.0, 0) is None


def test_retained_cpl_uses_observed_counts_only() -> None:
    assert retained_cpl(Decimal("100.00"), 20) == Decimal("5.00")
    assert retained_cpl(Decimal("100.00"), 0) is None
    with pytest.raises(ValueError):
        retained_cpl(Decimal("-1"), 2)


def test_default_snapshot_schedule_is_explicit_and_timezone_aware() -> None:
    published = datetime(2026, 10, 18, 10, tzinfo=UTC)
    planned = planned_snapshot_times(published)

    assert tuple(hours for hours, _ in planned) == DEFAULT_POST_SNAPSHOT_HOURS
    assert planned[0][1] == published + timedelta(hours=1)
    assert planned[-1][1] == published + timedelta(hours=72)
