"""Read consecutive automatic-cycle failures without modifying editorial state."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

WORKFLOW = "editorial-actions.yml"
AUTOMATIC_TITLE = "Editorial Actions · automatic"
RUNTIME_STEP = "Restore, claim, process, save and export"
STREAK_LIMIT = 7
ALERT_THRESHOLD = 3
CAUSES = {
    "storage_capacity": "не хватает места в хранилище",
    "snapshot_capacity": "исчерпан запас по размеру снимка",
    "storage_timeout": "истекло время ожидания ответа хранилища",
    "storage_unavailable": "SSH-хранилище недоступно",
    "storage_conflict": "конфликт при записи состояния",
    "storage_blob_missing": "хранилище не нашло снимок",
    "storage_state_missing": "хранилище не нашло состояние",
    "runtime_timeout": "окончился лимит времени рабочего цикла",
}


@dataclass(frozen=True)
class FailureStreak:
    cause: str
    run_id: str
    count: int


def _gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], stderr=subprocess.DEVNULL, timeout=8).decode()


def failure_cause(log: str, started_at: str, completed_at: str) -> str | None:
    start = datetime.fromisoformat(started_at)
    # GitHub reports the step's end in whole seconds and logs have fractions.
    limit = datetime.fromisoformat(completed_at) + timedelta(seconds=1)
    causes = set()
    for line in log.splitlines():
        timestamp, _, value = line.partition(" ")
        if not value.startswith('{"status": "blocked"'):
            continue
        try:
            if not start <= datetime.fromisoformat(timestamp) < limit:
                continue
            record = json.loads(value)
        except ValueError:
            continue
        if record.get("error_type") == "SnapshotCapacityError":
            causes.add("snapshot_capacity")
        elif record.get("error_type") == "SSHStorageError":
            code = record.get("storage_code")
            reason = record.get("storage_reason")
            if code == "storage_error" and reason == "timeout":
                causes.add("storage_timeout")
            elif code == "storage_error" and reason == "ssh_unavailable":
                causes.add("storage_unavailable")
            elif code == "storage_capacity":
                causes.add("storage_capacity")
            elif code in ("conflict", "blob_missing", "state_missing"):
                causes.add(f"storage_{code}")
    return causes.pop() if len(causes) == 1 else None


def _cycle_failure(repo: str, run_id: str, attempt: str, head: str) -> tuple[str, str | None]:
    result = json.loads(_gh("api", f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"))
    jobs = [job for job in result["jobs"] if job["name"] == "cycle"]
    if len(jobs) != 1:
        return "unknown", None
    job = jobs[0]
    if (
        job["status"] != "completed" or str(job["run_id"]) != run_id
        or str(job["run_attempt"]) != attempt or job["head_sha"] != head
    ):
        return "unknown", None
    conclusion = job["conclusion"]
    if conclusion not in {"failure", "timed_out"}:
        return conclusion or "unknown", None
    failed = [step for step in job["steps"] if step["name"] == RUNTIME_STEP]
    job_id = str(job["id"])
    if len(failed) != 1 or not job_id.isdigit():
        return "failure", None
    outcome = failed[0]["conclusion"]
    if conclusion == "timed_out" and outcome in {"timed_out", "cancelled"}:
        return "failure", "runtime_timeout"
    if outcome != "failure":
        return "failure", None
    # Capture the exact job log in memory. The CLI run viewer waits for the entire run,
    # but the independent retry clock can start before the parent deploy job finishes.
    log = _gh("api", "--allow-escape-sequences", f"repos/{repo}/actions/jobs/{job_id}/logs")
    return "failure", failure_cause(log, failed[0]["started_at"], failed[0]["completed_at"])


def recent_failure_streak(repo: str, *, through_run: str = "") -> FailureStreak | None:
    """Count confirmed matching failures, in reverse run order, up to the retry cap."""
    if through_run and not re.fullmatch(r"[1-9][0-9]{0,19}", through_run):
        return None
    try:
        response = json.loads(_gh("api", f"repos/{repo}/actions/workflows/{WORKFLOW}/runs"
                                        "?branch=main&per_page=25"))
        runs = sorted(response["workflow_runs"], key=lambda run: int(run["id"]), reverse=True)
        latest = ""
        cause: str | None = None
        count = 0
        checked = 0
        for run in runs:
            run_id = str(run["id"])
            if through_run and int(run_id) > int(through_run):
                continue
            if through_run and checked == 0 and run_id != through_run:
                return None
            if run["display_title"] != AUTOMATIC_TITLE:
                if run_id == through_run:
                    return None
                continue
            if run["status"] != "completed" and run_id != through_run:
                continue
            if run["conclusion"] == "success" or checked == 12:
                break
            checked += 1
            attempt = str(run["run_attempt"])
            if not run_id.isdigit() or not attempt.isdigit():
                break
            try:
                outcome, observed = _cycle_failure(repo, run_id, attempt, run["head_sha"])
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                break
            if outcome in {"cancelled", "skipped"} and run_id != through_run:
                continue
            if outcome != "failure" or observed is None or (cause and observed != cause):
                break
            if cause is None:
                latest, cause = run_id, observed
            count += 1
            if count == STREAK_LIMIT:
                break
        return FailureStreak(cause, latest, count) if cause is not None else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def retry_seconds(streak: FailureStreak | None) -> int:
    if streak is None or streak.count < 6:
        return 600
    return 1200 if streak.count == 6 else 1800


if __name__ == "__main__":
    run = sys.argv[1]
    print(retry_seconds(recent_failure_streak(os.environ["GITHUB_REPOSITORY"], through_run=run)))
