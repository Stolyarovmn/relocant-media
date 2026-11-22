import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from relocant_media.ops import editorial_failure_streak as history
from relocant_media.ops import editorial_watchdog as watchdog

START = "2026-11-22T12:05:00Z"
END = "2026-11-22T12:05:01Z"


def result_log(code, reason=None):
    data = {"status": "blocked", "error_type": "SSHStorageError", "storage_code": code}
    if reason:
        data["storage_reason"] = reason
    return f"2026-11-22T12:05:01.100Z {json.dumps(data)}"


def run(run_id, conclusion="failure", *, mode=history.AUTOMATIC_TITLE, attempt=1):
    return {"id": run_id, "conclusion": conclusion, "display_title": mode, "run_attempt": attempt,
            "head_sha": "a" * 40, "status": "completed" if conclusion else "in_progress"}


def fake_history(monkeypatch, runs, outcomes=None):
    requested = []
    outcomes = outcomes or {}

    def gh(*args):
        requested.append(args)
        if args[-1].endswith("/logs"):
            run_id = int(args[-1].split("/")[-2]) - 1000
            return result_log(outcomes.get(run_id, "storage_capacity"))
        if "/workflows/" in args[1]:
            return json.dumps({"workflow_runs": runs})
        _, run_id, _, attempt, _ = args[1].rsplit("/", 4)
        return json.dumps({"jobs": [{
            "id": int(run_id) + 1000,
            "name": "cycle",
            "status": "completed",
            "run_id": int(run_id),
            "run_attempt": int(attempt),
            "head_sha": "a" * 40,
            "conclusion": "failure",
            "steps": [{"name": history.RUNTIME_STEP, "conclusion": "failure",
                       "started_at": START, "completed_at": END}],
        }]})

    monkeypatch.setattr(history, "_gh", gh)
    return requested


def test_only_unambiguous_allowlisted_runtime_errors_count():
    capacity = result_log("storage_capacity")
    assert history.failure_cause(capacity, START, END) == "storage_capacity"
    timeout = result_log("storage_error", "timeout")
    assert history.failure_cause(timeout, START, END) == "storage_timeout"
    assert history.failure_cause(result_log("storage_error", "private details"), START, END) is None
    assert history.failure_cause(capacity + "\n" + result_log("conflict"), START, END) is None
    assert history.failure_cause(capacity.replace("12:05:01", "12:05:02"), START, END) is None
    generic_failure = capacity.replace("SSHStorageError", "ValueError")
    assert history.failure_cause(generic_failure, START, END) is None


@pytest.mark.parametrize(("count", "seconds"), [(3, 600), (6, 1200), (7, 1800)])
def test_history_escalates_at_three_and_backs_off_at_six(monkeypatch, count, seconds):
    requested = fake_history(monkeypatch, [run(101 - index, attempt=2) for index in range(count)])
    streak = history.recent_failure_streak(watchdog.REPO)
    assert streak == history.FailureStreak("storage_capacity", "101", count)
    assert history.retry_seconds(streak) == seconds
    assert any("/runs/101/attempts/2/jobs" in args[-1] for args in requested)
    assert any(args[-1].endswith("/actions/jobs/1101/logs") for args in requested)


def test_chain_anchors_on_its_finished_job_and_skips_unrelated_runs(monkeypatch):
    fake_history(monkeypatch, [
        run(103, "success"),
        run(102, None),
        run(101, "success", mode="Editorial Actions · digest"),
        run(100),
        run(99, "success"),
        run(98),
    ])
    assert history.recent_failure_streak(watchdog.REPO) is None
    assert history.recent_failure_streak(watchdog.REPO, through_run="102") == (
        history.FailureStreak("storage_capacity", "102", 2)
    )
    assert history.recent_failure_streak(watchdog.REPO, through_run="99") is None
    assert history.recent_failure_streak(watchdog.REPO, through_run="999") is None


def test_another_cause_or_missing_github_history_keeps_base_delay(monkeypatch):
    fake_history(monkeypatch, [run(101), run(100), run(99)], {100: "conflict"})
    assert history.recent_failure_streak(watchdog.REPO) == (
        history.FailureStreak("storage_capacity", "101", 1)
    )

    def unavailable(*_args):
        raise OSError("network or command unavailable")

    monkeypatch.setattr(history, "_gh", unavailable)
    assert history.retry_seconds(history.recent_failure_streak(watchdog.REPO)) == 600


@pytest.mark.asyncio
async def test_repeated_failure_alert_is_labeled_and_deduplicated(monkeypatch):
    streak = history.FailureStreak("storage_capacity", "101", 3)
    monkeypatch.setattr(watchdog, "recent_failure_streak", lambda _repo: streak)
    check = watchdog.check_failure_streak(watchdog.Check("storage_capacity"))
    assert check.status == "repeated_failure"
    assert check.streak == streak
    assert not watchdog.should_notify(
        replace(check, run_id="102", streak=replace(streak, run_id="102", count=4)), check.key,
    )
    assert watchdog.check_failure_streak(watchdog.Check("running", "102")).status == "running"
    monkeypatch.setenv("EDITORIAL_OWNER_CHAT_ID", "42")
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "42")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake")
    send = AsyncMock()
    monkeypatch.setattr(watchdog, "TelegramBotAPI", lambda _token, _client: type("Bot", (), {
        "send_message": send,
    })())
    await watchdog.notify_owner(check)
    chat, body = send.call_args.args
    assert chat == 42
    assert "Число сбоев подряд — 3" in body
    assert "не хватает места в хранилище" in body
    assert body.endswith("/actions/runs/101")
