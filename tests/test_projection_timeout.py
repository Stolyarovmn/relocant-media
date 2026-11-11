import json
import os
import subprocess
import sys
import time

import pytest
from test_actions_runtime import context as runtime_context
from test_actions_runtime import state as runtime_state

from relocant_media.ops import actions_runtime as runtime

context = runtime_context
state = runtime_state


def test_public_export_has_a_finite_deadline_and_keeps_raw_logs_private(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime.export_public(tmp_path / "private.sqlite3", tmp_path / "public.json",
                          "2026-11-11T20:00:00+00:00", tmp_path / "private-cycle")
    command, options = calls[0]
    assert command[:3] == [sys.executable, "-m", "relocant_media.ops.newsroom_export"]
    assert "--run-directory" in command
    assert options == {
        "check": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "timeout": 180,
    }


def test_hung_projection_is_killed_and_reaped_before_returning(tmp_path, monkeypatch):
    original_run = subprocess.run
    processes = []
    original_popen = subprocess.Popen

    def popen(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        processes.append(child)
        return child

    def hung(command, **kwargs):
        return original_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime.subprocess, "run", hung)
    monkeypatch.setattr(runtime, "PUBLIC_EXPORT_TIMEOUT_SECONDS", .15)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        runtime.export_public(tmp_path / "database", tmp_path / "output", "unused", None)
    assert time.monotonic() - started < 5
    assert len(processes) == 1 and processes[0].poll() is not None
    if os.name == "posix":
        with pytest.raises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)


@pytest.mark.parametrize("storage", ["drive", "ssh"])
def test_projection_timeout_commits_verified_snapshot_without_publishing_partial_output(
    context, tmp_path, monkeypatch, capsys, storage,
):
    store, drive = context
    checkpoints = []
    if storage == "ssh":
        backing = drive

        class CheckpointDrive(runtime.SSHStateClient):
            def __init__(self):
                pass

            def download(self, identifier):
                return backing.download(identifier)

            def upload(self, path, folder):
                return backing.upload(path, folder)

            def checkpoint(self, ready, busy_sha, path):
                checkpoints.append((ready.copy(), busy_sha))
                return "a" * 64

        drive = CheckpointDrive()
    store.state["last_cycle"] = {"status": "PRIMARY_OK", "observed_at": "previous"}
    before = store.state["snapshot"].copy()
    public = tmp_path / "_site/data.json"
    child_runs = []

    def child(command, environment):
        child_runs.append(True)
        return 0

    def timeout(database, output, observed_at, run_dir):
        # Even an incomplete file must not reach Pages after the subprocess deadline.
        output.write_text('{"private": "UNPUBLISHABLE-PARTIAL"}', encoding="utf-8")
        assert store.state["status"] == "busy"
        if storage == "ssh":
            assert len(checkpoints) == 1
        raise subprocess.TimeoutExpired(["private-command", "PRIVATE-TOKEN"], 180,
                                        output="PRIVATE-TEXT", stderr="PRIVATE-ERROR")

    monkeypatch.setattr(runtime, "run_child", child)
    monkeypatch.setattr(runtime, "export_public", timeout)
    result = runtime.execute(
        store, drive, work=tmp_path / "work", output=public, folder="folder_id_1",
        mode="editorial", environment={
            "ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
            "GITHUB_REF": "refs/heads/main",
        },
    )

    assert result == 1
    assert child_runs == [True]
    assert store.state["status"] == "ready"
    assert store.state["last_exit_code"] == 0
    assert store.state["snapshot"]["sha256"] == before["sha256"]
    assert store.state["last_cycle"] == {"status": "PRIMARY_OK", "observed_at": "previous"}
    if storage == "ssh":
        assert checkpoints[0][0] == store.state
        assert checkpoints[0][1] != store.sha
    runtime.validate_ready(store.state)
    assert not public.exists()
    assert not (tmp_path / "work/public-projection.json").exists()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert "UNPUBLISHABLE" not in output.out + output.err
    records = [json.loads(line) for line in output.err.splitlines()]
    assert {"projection_error_type": "TimeoutExpired"} in records
    expected = [
        "worker_started", "worker_completed", "snapshot_started", "snapshot_verified",
        *(["recovery_checkpoint_started"] if storage == "ssh" else []),
        "projection_started", "projection_failed", "state_commit_started", "state_committed",
    ]
    assert [record["runtime_phase"] for record in records if "runtime_phase" in record] == expected
    assert ({"recovery_checkpoint_sha256": "a" * 64} in records) == (storage == "ssh")
