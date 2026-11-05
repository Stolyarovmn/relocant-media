import subprocess
from pathlib import Path

import pytest

from relocant_media.ops import ssh_state_client
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient, SSHStorageError


def _client() -> SSHStateClient:
    return SSHStateClient(SSHConfig("203.0.113.5", "relocant-state", 22, Path("key"), Path("kh")))


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (subprocess.TimeoutExpired("ssh", 180), "timeout"),
        (OSError("no ssh"), "ssh_unavailable"),
    ],
)
def test_transport_failures_carry_a_safe_reason(monkeypatch, raised, reason) -> None:
    def fail(*args, **kwargs):
        raise raised

    monkeypatch.setattr(ssh_state_client.subprocess, "run", fail)

    with pytest.raises(SSHStorageError) as caught:
        _client().exchange({"op": "health"})

    assert caught.value.code == "storage_error" and caught.value.reason == reason
    assert str(caught.value) == f"SSH storage blocked: storage_error ({reason})"


def test_unknown_reason_is_dropped() -> None:
    error = SSHStorageError("storage_error", "/root/secret path")
    assert error.reason is None and "secret" not in str(error)
