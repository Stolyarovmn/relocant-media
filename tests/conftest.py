from datetime import UTC, datetime

import pytest

from relocant_media.domain import quiet_hours


@pytest.fixture(autouse=True)
def _daytime_channel_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payload-тесты не должны зависеть от того, ночь ли сейчас в Белграде."""
    monkeypatch.setattr(quiet_hours, "utc_now", lambda: datetime(2026, 11, 20, 11, tzinfo=UTC))
