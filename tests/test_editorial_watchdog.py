import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from relocant_media.ops import editorial_watchdog as watchdog
from relocant_media.ops.ssh_state_client import SSHStorageError

NOW = datetime(2026, 11, 13, 8, 45, tzinfo=UTC)


def test_healthy_stale_and_storage_unavailable(tmp_path):
    client = Mock()
    client.read.return_value = ({"status": "ready", "last_editorial_at": NOW.isoformat()}, "sha")
    assert watchdog.inspect(client, NOW, tmp_path, allow_recovery=True).status == "healthy"
    assert watchdog.inspect(
        client, NOW + timedelta(hours=1), tmp_path, allow_recovery=True,
    ).status == "stale"
    client.read.side_effect = SSHStorageError("storage_error", "timeout")
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
    client.read.return_value = ({
        "status": "busy", "run_id": "123", "run_attempt": "2", "started_at": NOW.isoformat(),
    }, "sha")
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
