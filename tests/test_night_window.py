"""#359: несрочное, готовое ночью, выходит утром с 08:00 и не сгорает по сроку."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session
from test_channel_delivery import reviewed as reviewed_fixture

from relocant_media.domain import quiet_hours
from relocant_media.domain.pacing import delivery_order, morning_deadline, morning_release
from relocant_media.ops import server_cycle
from relocant_media.services.material_lifecycle import (
    delivery_deadline,
    hold_until_morning,
    lifecycle_state,
)
from relocant_media.storage.models import EditorialRun, RawItem

reviewed = reviewed_fixture
BELGRADE = ZoneInfo("Europe/Belgrade")


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=BELGRADE)


@pytest.mark.parametrize(("now", "release"), [
    (local(2026, 11, 22, 23, 0), local(2026, 11, 23, 8, 0)),
    (local(2026, 11, 22, 23, 40), local(2026, 11, 23, 8, 0)),
    (local(2026, 11, 23, 0, 5), local(2026, 11, 23, 8, 0)),
    (local(2026, 11, 23, 7, 59), local(2026, 11, 23, 8, 0)),
    # Переход на летнее время: 08:00 остаётся местным утром.
    (local(2027, 3, 28, 1, 30), local(2027, 3, 28, 8, 0)),
])
def test_night_story_waits_for_eight_belgrade(now, release):
    assert morning_release(category="culture", urgent=False, now=now.astimezone(UTC)) == release
    assert morning_deadline(release) == release.replace(hour=12)


@pytest.mark.parametrize("now", [local(2026, 11, 23, 8, 0), local(2026, 11, 23, 15, 0),
                                 local(2026, 11, 23, 22, 59)])
def test_daytime_story_is_not_held(now):
    assert morning_release(category="culture", urgent=False, now=now) is None


@pytest.mark.parametrize(("category", "urgent"), [
    ("transport", False), ("utilities", False), ("weather", False), ("culture", True),
])
def test_urgent_story_goes_out_at_night(category, urgent):
    assert morning_release(category=category, urgent=urgent,
                           now=local(2026, 11, 23, 2, 0)) is None


def test_hold_extends_only_ready_material_and_only_forward(reviewed):
    session, run = reviewed
    now = datetime.now(UTC)
    lifecycle_state(session, run.raw_item_id, now=now)
    before = delivery_deadline(session, run.raw_item_id)
    assert before is not None
    later = now + timedelta(hours=9)
    assert hold_until_morning(session, run.raw_item_id, now=now, deadline=later) is True
    assert delivery_deadline(session, run.raw_item_id) == later
    # Повторный вызов с более ранним сроком ничего не сокращает.
    assert hold_until_morning(session, run.raw_item_id, now=now, deadline=before) is True
    assert delivery_deadline(session, run.raw_item_id) == later
    state = lifecycle_state(session, run.raw_item_id, now=now + timedelta(hours=8))
    assert state["state"] == "ready" and state["next_action"] == "deliver"


@pytest.mark.asyncio
async def test_night_story_is_held_without_delivery_attempt(reviewed, monkeypatch):
    session, run = reviewed
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    night = local(2026, 11, 23, 2, 0).astimezone(UTC)
    monkeypatch.setattr(quiet_hours, "utc_now", lambda: night)
    asked: list[dict] = []

    def release(**kwargs):
        asked.append(kwargs)
        return local(2026, 11, 23, 8, 0)

    monkeypatch.setattr(server_cycle, "morning_release", release)
    held: list[int] = []
    monkeypatch.setattr(
        "relocant_media.services.material_lifecycle.hold_until_morning",
        lambda session, raw_id, **kwargs: held.append(raw_id) or True)

    def no_attempt(*args, **kwargs):
        raise AssertionError("Ночной пост не должен расходовать попытку доставки")

    monkeypatch.setattr(
        "relocant_media.services.material_lifecycle.before_delivery_attempt", no_attempt)
    blocked: list = []
    assert await server_cycle.publish_pending(
        session.get_bind(), enabled=True, blocked=blocked) == []
    assert blocked == [] and held == [run.raw_item_id]
    assert asked == [{"category": "transport", "urgent": False, "now": night}]
    with Session(session.get_bind()) as check:
        assert check.get(EditorialRun, run.raw_item_id).delivery_status == "pending"


def test_today_news_goes_before_yesterday_backlog():
    now = local(2026, 11, 23, 8, 5).astimezone(UTC)
    yesterday = local(2026, 11, 22, 21, 0).astimezone(UTC)
    today = local(2026, 11, 23, 7, 30).astimezone(UTC)
    naive_today = today.replace(tzinfo=None)  # SQLite отдаёт UTC без зоны
    assert delivery_order([(1, yesterday), (2, today), (3, None), (4, naive_today)],
                          now=now) == [2, 4, 1, 3]
    # Днём всё «сегодня», порядок остаётся по поступлению.
    assert delivery_order([(5, today), (6, today)], now=now) == [5, 6]


@pytest.mark.asyncio
async def test_morning_cycle_tries_today_story_first(reviewed, monkeypatch):
    session, run = reviewed
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    morning = local(2026, 11, 23, 8, 5).astimezone(UTC)
    monkeypatch.setattr(quiet_hours, "utc_now", lambda: morning)
    old = session.get(RawItem, run.raw_item_id)
    old.published_at = local(2026, 11, 22, 21, 0).astimezone(UTC)
    fresh = RawItem(source_id=old.source_id, external_id=old.external_id + "-today",
                    url=old.url + "?today", title=old.title, body=old.body,
                    published_at=local(2026, 11, 23, 7, 30).astimezone(UTC),
                    published_at_precision=old.published_at_precision)
    session.add(fresh)
    session.flush()
    session.add(EditorialRun(raw_item_id=fresh.id, outcome=dict(run.outcome),
                             evidence=dict(run.evidence),
                             editorial_rule_version=run.editorial_rule_version,
                             delivery_status="pending"))
    session.commit()
    tried: list[int] = []
    monkeypatch.setattr(
        "relocant_media.services.material_lifecycle.before_delivery_attempt",
        lambda session, raw_id, **kwargs: tried.append(raw_id) and False)
    assert await server_cycle.publish_pending(session.get_bind(), enabled=True) == []
    assert tried == [fresh.id, run.raw_item_id]
