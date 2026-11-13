"""Verify and restore a saved Actions checkpoint inside the canonical workflow."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relocant_media.ops.actions_runtime import REPO
from relocant_media.ops.editorial_cycle_observation import observe_cycle
from relocant_media.ops.recovery_checkpoint import prepare
from relocant_media.ops.sqlite_restore import restore
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


def rollback(
    client: SSHStateClient, run_id: str, snapshot_at: str, last_public_post: int, work: Path,
) -> dict[str, Any]:
    """Вернуть указатель к последнему целому снимку после сверки с каналом."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id) or last_public_post < 1:
        raise ValueError("exact interrupted run and public message ID required")
    busy, expected = client.read()
    manifest = busy.get("snapshot", {})
    attempt = busy.get("run_attempt")
    generation = busy.get("attempt")
    if (busy.get("status") != "busy" or busy.get("run_id") != run_id
            or not isinstance(attempt, str) or not isinstance(generation, str)
            or manifest.get("created_at") != snapshot_at
            or busy.get("cutover", {}).get("server_fenced") is not True):
        raise ValueError("run or last saved snapshot differs")
    verify_stopped_run(run_id, attempt)
    work.mkdir(parents=True, exist_ok=False, mode=0o700)
    snapshot = work / "editorial.sqlite3"
    snapshot.write_bytes(client.download(manifest["sha256"]))
    manifest_path = work / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restore(snapshot, manifest_path, work / "verified")
    verified = work / "verified" / "editorial.sqlite3"
    with closing(sqlite3.connect(verified.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if (db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA foreign_key_check").fetchone() is not None):
            raise ValueError("rollback snapshot integrity check failed")
        latest = db.execute(
            "SELECT max(telegram_message_id) FROM post WHERE city='belgrade'",
        ).fetchone()[0]
        unsettled = db.execute(
            "SELECT 1 FROM editorial_run WHERE delivery_status IN ('sending','uncertain') LIMIT 1",
        ).fetchone()
    if latest != last_public_post or unsettled is not None:
        raise ValueError("snapshot has different or unresolved public deliveries")
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as http:
        page = http.get("https://t.me/s/eduvbelgrad")
        page.raise_for_status()
    visible = max(map(int, re.findall(r'data-post="eduvbelgrad/([1-9]\d*)"', page.text)), default=0)
    if visible != last_public_post:
        raise ValueError("public channel differs from verified snapshot")

    ready = dict(busy, status="ready", generation=generation)
    for field in ("run_id", "run_attempt", "started_at"):
        ready.pop(field, None)
    audit = {
        "kind": "rollback_confirmed_by_owner", "run_id": run_id, "run_attempt": attempt,
        "rollback_at": datetime.now(UTC).isoformat(), "snapshot_created_at": snapshot_at,
        "public_message_id_verified": last_public_post,
    }
    ready["last_manual_recovery"] = audit
    if client.read() != (busy, expected):
        raise ValueError("busy pointer changed during rollback verification")
    digest = client.write(ready, expected)
    if client.read() != (ready, digest):
        raise ValueError("rollback pointer readback differs")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--checkpoint", default="")
    parser.add_argument("--rollback-snapshot-at", default="")
    parser.add_argument("--rollback-public-post", default="")
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
        if args.rollback_snapshot_at and args.rollback_public_post and not args.checkpoint:
            result = rollback(
                client, args.run_id, args.rollback_snapshot_at, int(args.rollback_public_post),
                args.work,
            )
        elif args.checkpoint and not args.rollback_snapshot_at and not args.rollback_public_post:
            result = recover(client, args.run_id, args.checkpoint, args.work)
        else:
            raise ValueError("choose a checkpoint or explicitly confirmed rollback")
    except Exception as error:
        print(json.dumps({"recovery_error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"verified_recovery": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
