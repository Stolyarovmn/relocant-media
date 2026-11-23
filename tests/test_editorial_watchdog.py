import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from relocant_media.ops import editorial_watchdog as watchdog
from relocant_media.ops.ssh_state_client import SSHStorageError

NOW = datetime(2026, 11, 13, 8, 45, tzinfo=UTC)


def test_healthy_stale_and_storage_unavailable(tmp_path):
    client = Mock()
    ready = {"status": "ready", "last_editorial_at": NOW.isoformat()}
    client.read_status.return_value = (ready, "sha", None)
    assert watchdog.inspect(client, NOW, tmp_path, allow_recovery=True).status == "healthy"
    assert watchdog.inspect(
        client, NOW + timedelta(hours=1), tmp_path, allow_recovery=True,
    ).status == "stale"
    client.read_status.return_value = (ready, "sha", True)
    warning = watchdog.inspect(client, NOW, tmp_path, allow_recovery=True)
    assert warning.status == "storage_capacity"
    assert watchdog.should_notify(warning, "")
    assert not watchdog.should_notify(warning, warning.key)
    client.write.assert_not_called()
    client.read_status.side_effect = SSHStorageError("storage_error", "timeout")
    assert watchdog.inspect(client, NOW, tmp_path, allow_recovery=True).status == "storage"


@pytest.mark.parametrize(("run_status", "checkpoint", "allowed", "expected"), [
    ("in_progress", None, True, "running"),
    ("completed", None, True, "busy"),
    ("completed", "a" * 64, False, "recovery_waiting_ci"),
    ("completed", "a" * 64, True, "restored"),
])
def test_only_stopped_run_with_checkpoint_can_be_recovered(
    tmp_path, monkeypatch, run_status, checkpoint, allowed, expected,
):
    client = Mock()
    client.read_status.return_value = ({
        "status": "busy", "run_id": "123", "run_attempt": "2", "started_at": NOW.isoformat(),
    }, "sha", None)
    metadata = {"id": 123, "run_attempt": 2, "path": watchdog.WORKFLOW,
                "head_branch": "main", "status": run_status}
    monkeypatch.setattr(watchdog, "run_metadata", lambda run_id: metadata)
    monkeypatch.setattr(watchdog, "checkpoint_from_run", lambda run_id, attempt: checkpoint)
    recovery = Mock()
    monkeypatch.setattr(watchdog, "recover", recovery)

    assert watchdog.inspect(client, NOW, tmp_path, allow_recovery=allowed).status == expected
    if expected == "restored":
        recovery.assert_called_once_with(client, "123", checkpoint, tmp_path)
    else:
        recovery.assert_not_called()
    client.write.assert_not_called()


def test_checkpoint_read_from_exact_attempt_and_ambiguous_log_rejected(monkeypatch):
    checkpoint = '{"recovery_checkpoint_sha256": "' + "a" * 64 + '"}'
    gh = Mock(side_effect=['{"jobs":[{"id":456,"name":"cycle"}]}', checkpoint])
    monkeypatch.setattr(watchdog, "gh", gh)
    assert watchdog.checkpoint_from_run("123", "2") == "a" * 64
    assert gh.call_args.args[-5:] == ("--attempt", "2", "--job", "456", "--log")
    gh.side_effect = ['{"jobs":[{"id":456,"name":"cycle"}]}',
                      checkpoint + "\n" + checkpoint.replace("a" * 64, "b" * 64)]
    assert watchdog.checkpoint_from_run("123", "2") is None


def test_alert_is_deduplicated_and_healthy_start_is_silent():
    incident = watchdog.Check("busy", "123")
    healthy = watchdog.Check("healthy")
    assert watchdog.should_notify(incident, "")
    assert not watchdog.should_notify(incident, incident.key)
    assert watchdog.should_notify(watchdog.Check("busy", "456"), incident.key)
    assert watchdog.should_notify(healthy, incident.key)
    assert not watchdog.should_notify(healthy, "")
    assert not watchdog.should_notify(healthy, healthy.key)


def test_notification_requires_exact_private_owner(monkeypatch):
    monkeypatch.setenv("EDITORIAL_OWNER_CHAT_ID", "42")
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "43")
    with pytest.raises(ValueError, match="private owner"):
        asyncio.run(watchdog.notify_owner(watchdog.Check("busy", "123")))


def test_morning_alert_keeps_legacy_queue_state_and_is_deduplicated_by_day(
    tmp_path, monkeypatch,
):
    file = tmp_path / "watchdog"
    status = watchdog.Check("healthy")
    file.write_text(status.key, encoding="utf-8")
    now = datetime(2026, 11, 22, 10, tzinfo=UTC)
    client = Mock()
    client.read_status.return_value = ({"status": "ready"}, "sha", None)
    assert watchdog.inspect(client, now, tmp_path, allow_recovery=False).morning is None
    client.read_status.reset_mock()
    check = watchdog.inspect(client, now, tmp_path, allow_recovery=False, check_digests=True)
    client.read_status.assert_called_once_with()
    client.write.assert_not_called()

    send = AsyncMock()
    monkeypatch.setattr(watchdog, "notify_owner", send)
    assert watchdog.update_alerts(check, file)
    send.assert_awaited_once_with(None, check.morning, None)
    assert not watchdog.update_alerts(check, file)
    send.assert_awaited_once()
    assert watchdog.AlertState.read(file).queue == status.key
    next_day = watchdog.inspect(client, now + timedelta(days=1), tmp_path,
                                allow_recovery=False, check_digests=True)
    assert watchdog.update_alerts(next_day, file)
    assert send.await_count == 2


def test_confirmed_morning_does_not_notify_owner(tmp_path, monkeypatch):
    now = datetime(2026, 11, 22, 10, tzinfo=UTC)
    client = Mock()
    client.read_status.return_value = (
        {"status": "ready", "digest_receipts": {
            "belgrade:2026-11-22:morning": {"state": "sent", "message_id": 152},
        }}, "sha", None,
    )
    check = watchdog.inspect(client, now, tmp_path, allow_recovery=False, check_digests=True)
    assert check.morning.status == "sent"
    send = AsyncMock()
    monkeypatch.setattr(watchdog, "notify_owner", send)
    assert watchdog.update_alerts(check, tmp_path / "watchdog")
    send.assert_not_awaited()


def test_redundant_check_targets_the_named_morning_and_does_not_recover_queue(
    tmp_path, monkeypatch,
):
    now = datetime(2026, 11, 22, 10, tzinfo=UTC)
    client = Mock()
    client.read_status.return_value = ({"status": "busy"}, "sha", None)
    queue = Mock(side_effect=AssertionError("morning-only check must not touch writer recovery"))
    monkeypatch.setattr(watchdog, "_inspect_queue", queue)
    check = watchdog.inspect(client, now, tmp_path, allow_recovery=False,
                             check_digests=True, morning_date=now.date())
    assert check.status == "morning_only" and check.morning.status == "missed"
    assert not watchdog.should_notify(check, "")
    yesterday = watchdog.inspect(client, now, tmp_path, allow_recovery=False,
                                 check_digests=True, morning_date=date(2026, 11, 21))
    assert yesterday.morning is None
    client.read_status.side_effect = SSHStorageError("storage_error", "timeout")
    with pytest.raises(SSHStorageError):
        watchdog.inspect(client, now, tmp_path, allow_recovery=False,
                         check_digests=True, morning_date=now.date())


def test_finite_digest_schedule_alert_survives_storage_failure_without_repeats(
    tmp_path, monkeypatch,
):
    cutoff = datetime(2026, 11, 27, 19, 30, tzinfo=UTC)
    moved_cutoff = cutoff + timedelta(days=1)
    client = Mock()
    client.read_status.side_effect = SSHStorageError("storage_error", "timeout")
    monkeypatch.setattr(watchdog, "digest_end_warning", lambda now: cutoff)
    check = watchdog.inspect(client, NOW, tmp_path, allow_recovery=False, check_digests=True)
    assert check.status == "storage" and check.digest_end == cutoff
    sender = AsyncMock(side_effect=[RuntimeError("Telegram"), None, None])
    monkeypatch.setattr(watchdog, "notify_owner", sender)
    path = tmp_path / "watchdog"
    with pytest.raises(RuntimeError):
        watchdog.update_alerts(check, path)
    assert not path.exists()
    assert watchdog.update_alerts(check, path)
    assert not watchdog.update_alerts(check, path)
    assert sender.await_count == 2
    assert sender.await_args.args == (check, None, cutoff)
    assert watchdog.update_alerts(watchdog.Check("storage", digest_end=moved_cutoff), path)
    assert sender.await_args.args == (None, None, moved_cutoff)
    client.write.assert_not_called()


def test_owner_notice_reports_a_confirmed_reason_in_one_private_message(monkeypatch):
    now = datetime(2026, 11, 22, 10, tzinfo=UTC)
    morning = watchdog.morning_delivery({"status": "ready", "last_morning_digest_attempt": {
        "slot": "belgrade:2026-11-22:morning", "reason": "insufficient_items",
    }}, now)
    monkeypatch.setenv("EDITORIAL_OWNER_CHAT_ID", "42")
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "42")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    bot = Mock(send_message=AsyncMock())
    monkeypatch.setattr(watchdog, "TelegramBotAPI", lambda *_args: bot)
    cutoff = datetime(2026, 11, 27, 20, 30, tzinfo=UTC)
    asyncio.run(watchdog.notify_owner(watchdog.Check("stale"), morning, cutoff))
    bot.send_message.assert_awaited_once()
    chat_id, text = bot.send_message.await_args.args
    assert chat_id == 42
    assert "22.11.2026" in text and "11:00 по Белграду" in text
    assert "не хватило материалов" in text and "больше часа" in text
    assert "27.11.2026 в 20:30 по Белграду" in text


def test_failed_current_main_cycle_can_wake_the_storage_watchdog():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/editorial-watchdog.yml"
    workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["on"]["workflow_run"] == {
        "workflows": ["Editorial Actions"], "branches": ["main"], "types": ["completed"],
    }
    assert "github.event_name != 'workflow_run'" in workflow["jobs"]["intake"]["if"]
    job = workflow["jobs"]["check"]
    assert all(clause in job["if"] for clause in (
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_sha == github.sha",
        "github.event.workflow_run.path == '.github/workflows/editorial-actions.yml'",
        "github.event.workflow_run.conclusion == 'failure'",
        "github.event.workflow_run.conclusion == 'timed_out'",
        "vars.ACTIONS_CUTOVER_CONFIRMED == 'true'",
    ))
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    assert all("artifact" not in step.get("uses", "") for step in job["steps"])
    env = next(step["env"] for step in job["steps"] if step.get("id") == "check")
    assert env["EDITORIAL_SEND_ENABLED"] == "${{ vars.ACTIONS_SEND_ENABLED }}"
    assert env["EDITORIAL_CHANNEL_MODE"] == "public"
