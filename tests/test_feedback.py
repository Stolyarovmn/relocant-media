from datetime import UTC, datetime

import pytest

from relocant_media.analytics.feedback import (
    feedback_summary,
    normalize_observation,
    save_observation,
)
from relocant_media.services.audience_observation import observe_audience

NOW = datetime(2026, 10, 21, 6, tzinfo=UTC)


def sample(hours=24, corrected=False):
    from datetime import timedelta

    observation = dict(
        source="telegram_web_ui",
        channel_id=-1003967014133,
        observed_at=NOW.isoformat(),
        subscribers=2,
        posts=[
            dict(message_id=8, views_raw="Views: 2", reactions=None, comments=None, forwards=None)
        ],
    )
    metadata = [
        dict(
            message_id=8,
            post_id=1,
            published_at=(NOW - timedelta(hours=hours)).isoformat(),
            category="events",
            editorial_rule_version="belgrade-editorial-v2",
            has_correction=corrected,
        )
    ]
    return observation, metadata


def test_missing_engagement_stays_unknown_and_small_sample_cannot_train():
    observed, metadata = sample()
    payload = normalize_observation(observed, metadata, now=NOW)
    row = payload["posts"][0]
    assert row["views"] == 2 and row["horizon_hours"] == 24
    assert row["reactions"] is None and row["comments"] is None and row["forwards"] is None
    summary = feedback_summary([payload, payload])
    assert summary["comparable_posts"] == 1 and not summary["automatic_adaptation"]
    assert summary["status"] == "collecting_baseline"


@pytest.mark.parametrize(
    "hours,corrected,horizon,comparable",
    [(24, False, 24, True), (28, False, None, False), (48, False, 48, True), (24, True, 24, False)],
)
def test_real_age_and_corrections_control_comparability(hours, corrected, horizon, comparable):
    row = normalize_observation(*sample(hours, corrected), now=NOW)["posts"][0]
    assert (row["horizon_hours"], row["comparable"]) == (horizon, comparable)
    assert row["age_hours"] == hours


def test_journal_is_idempotent_and_keeps_unknown_views(tmp_path):
    observation, metadata = sample()
    observation["posts"][0]["views_raw"] = None
    payload = normalize_observation(observation, metadata, now=NOW)
    first = save_observation(tmp_path, payload)
    assert save_observation(tmp_path, payload) == first
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert payload["posts"][0]["views"] is None


@pytest.mark.parametrize("kind", ["other_channel", "unknown_post", "duplicate", "fake_counter"])
def test_invalid_observations_are_not_imported(kind):
    observation, metadata = sample()
    if kind == "other_channel":
        observation["channel_id"] = -100999
    if kind == "unknown_post":
        observation["posts"][0]["message_id"] = 900
    if kind == "duplicate":
        observation["posts"] *= 2
    if kind == "fake_counter":
        observation["posts"][0]["reactions"] = True
    with pytest.raises(ValueError):
        normalize_observation(observation, metadata, now=NOW)


class Bot:
    def __init__(self, count=2, error=None):
        self.count, self.error, self.calls = count, error, []

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        if self.error:
            raise self.error
        return self.count


@pytest.mark.asyncio
async def test_bot_collects_members_without_claiming_views_or_reading_messages():
    bot = Bot()
    result = await observe_audience(bot)
    assert result["subscribers"] == 2 and result["post_views"] is None
    assert bot.calls == [("getChatMemberCount", {"chat_id": -1003967014133})]


@pytest.mark.asyncio
@pytest.mark.parametrize("bot", [Bot(count=True), Bot(error=RuntimeError("secret"))])
async def test_metrics_failure_is_explicit_without_leaking_error_text(bot):
    result = await observe_audience(bot)
    assert result["status"] == "unavailable" and result["subscribers"] is None
    assert "secret" not in str(result)
