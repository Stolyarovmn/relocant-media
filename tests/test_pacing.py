"""#337: несрочные посты не выходят пачкой, но и не сгорают из-за паузы."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_channel_delivery import reviewed as reviewed_fixture

from relocant_media.domain.pacing import MIN_GAP, PACING_REASON, pacing_hold
from relocant_media.ops import server_cycle
from relocant_media.storage.models import EditorialRun

reviewed = reviewed_fixture
NOW = datetime(2026, 11, 21, 12, tzinfo=UTC)


def hold(**overrides):
    values = dict(category="culture", urgent=False, now=NOW,
                  last_sent_at=NOW - timedelta(minutes=5), deadline_at=NOW + timedelta(hours=4))
    values.update(overrides)
    return pacing_hold(**values)


def test_recent_post_defers_non_urgent_story():
    assert hold() == PACING_REASON


@pytest.mark.parametrize("overrides", [
    {"category": "transport"}, {"category": "utilities"}, {"category": "weather"},
    {"urgent": True},
    {"last_sent_at": None},
    {"last_sent_at": NOW - MIN_GAP},
    # Пауза никогда не съедает срок жизни материала.
    {"deadline_at": NOW + timedelta(minutes=60)},
    {"deadline_at": None},
])
def test_pacing_never_delays_urgent_or_expiring(overrides):
    assert hold(**overrides) is None


@pytest.mark.asyncio
async def test_paced_story_is_not_a_delivery_attempt(reviewed, monkeypatch):
    session, run = reviewed
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    asked: list[dict] = []

    def paced(**kwargs):
        asked.append(kwargs)
        return PACING_REASON

    monkeypatch.setattr(server_cycle, "pacing_hold", paced)

    def no_attempt(*args, **kwargs):
        raise AssertionError("Отложенный пост не должен расходовать попытку доставки")

    monkeypatch.setattr(
        "relocant_media.services.material_lifecycle.before_delivery_attempt", no_attempt)
    blocked: list = []
    assert await server_cycle.publish_pending(
        session.get_bind(), enabled=True, blocked=blocked) == []
    assert blocked == []
    assert [call["category"] for call in asked] == ["transport"]
    with Session(session.get_bind()) as check:
        assert check.get(EditorialRun, run.raw_item_id).delivery_status == "pending"
