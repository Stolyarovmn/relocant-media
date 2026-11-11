"""Verify and restore a saved Actions checkpoint inside the canonical workflow."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from relocant_media.ops.actions_runtime import REPO
from relocant_media.ops.editorial_cycle_observation import observe_cycle
from relocant_media.ops.recovery_checkpoint import prepare
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient


def verify_stopped_run(run_id: str, attempt: str) -> None:
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/actions/runs/{run_id}"],
        check=True, capture_output=True, timeout=30,
    )
    run = json.loads(result.stdout)
    if (str(run["id"]) != run_id or str(run["run_attempt"]) != attempt
            or run["status"] != "completed" or run["head_branch"] != "main"
            or run["path"] != ".github/workflows/editorial-actions.yml"):
        raise ValueError("interrupted writer has not been verified as stopped")


def recover(client: SSHStateClient, run_id: str, checkpoint_sha: str,
            work: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id) or not re.fullmatch(
        r"[a-f0-9]{64}", checkpoint_sha,
    ):
        raise ValueError("exact interrupted run ID and checkpoint SHA256 required")
    busy, expected = client.read()
    if (busy.get("status") != "busy" or busy.get("run_id") != run_id
            or not isinstance(busy.get("run_attempt"), str)):
        raise ValueError("interrupted run does not own the current busy pointer")
    verify_stopped_run(run_id, busy["run_attempt"])
    checkpoint = json.loads(client.download(checkpoint_sha))
    if checkpoint.get("expected_busy_sha256") != expected:
        raise ValueError("checkpoint does not match current busy pointer")
    work.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot = work / "editorial.sqlite3"
    snapshot.write_bytes(client.download(checkpoint["state"]["snapshot"]["sha256"]))
    prepare(checkpoint, busy, snapshot, work / "prepared", writers_fenced=True)
    state = checkpoint["state"]
    if client.read() != (busy, expected):
        raise ValueError("busy pointer changed during recovery verification")
    digest = client.write(state, expected)
    if client.read() != (state, digest):
        raise ValueError("restored pointer readback differs")
    report = {"recovered_run_id": run_id, "snapshot_sha256": state["snapshot"]["sha256"],
              "saved_worker_exit": state["last_exit_code"]}
    try:
        report["editorial"] = observe_cycle(
            work / "prepared/verified/editorial.sqlite3", busy["started_at"],
        )
    except Exception as error:
        report["observation_error_type"] = type(error).__name__
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    if (os.environ.get("GITHUB_REPOSITORY") != REPO
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("ACTIONS_CUTOVER_CONFIRMED") != "true"
            or os.environ.get("EDITORIAL_STORAGE_BACKEND") != "ssh"
            or os.environ.get("GITHUB_RUN_ID") == args.run_id):
        parser.error("recovery requires a new manual canonical Actions run")
    try:
        client = SSHStateClient(SSHConfig.from_environment(os.environ))
        result = recover(client, args.run_id, args.checkpoint, args.work)
    except Exception as error:
        print(json.dumps({"recovery_error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"verified_recovery": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
