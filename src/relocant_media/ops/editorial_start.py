"""Check the exact main CI and replace a stopped editorial start once."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

CI_WORKFLOW = "ci.yml"
EDITORIAL_WORKFLOW = "editorial-actions.yml"
RETRY_TITLES = {
    "auto": "Editorial Actions · automatic",
    "editorial": "Editorial Actions · editorial",
    "intake": "Editorial Actions · intake",
    "digest": "Editorial Actions · digest",
}
GPT_RETRY_MODES = frozenset({"fallback-preview", "fallback"})
RETRY_MODES = RETRY_TITLES.keys() | GPT_RETRY_MODES
ACTIVE = frozenset({"in_progress", "queued", "requested", "waiting", "pending"})
StartStatus = Literal["ready", "superseded", "ci_pending", "ci_failed"]


def api(repo: str, path: str) -> Any:
    return json.loads(subprocess.check_output(["gh", "api", f"repos/{repo}/{path}"], timeout=8))


def main_sha(repo: str) -> str:
    value = api(repo, "git/ref/heads/main")["object"]["sha"]
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("invalid main ref")
    return value


def ci_status(repo: str, sha: str) -> StartStatus:
    response = api(repo, f"actions/workflows/{CI_WORKFLOW}/runs"
                   f"?branch=main&event=push&head_sha={sha}&per_page=1")
    runs = response["workflow_runs"]
    if not isinstance(runs, list):
        raise ValueError("invalid CI response")
    if not runs:
        return "ci_pending"
    run = runs[0]
    if (run["head_sha"] != sha or run["head_branch"] != "main"
            or run["event"] != "push" or run["path"] != f".github/workflows/{CI_WORKFLOW}"):
        raise ValueError("CI does not match the exact main push")
    status = run["status"]
    if status in ACTIVE:
        return "ci_pending"
    if status != "completed":
        raise ValueError("unknown CI status")
    return "ready" if run["conclusion"] == "success" else "ci_failed"


def start_status(repo: str, expected: str) -> StartStatus:
    if main_sha(repo) != expected:
        return "superseded"
    status = ci_status(repo, expected)
    return status if main_sha(repo) == expected else "superseded"


def already_scheduled(repo: str, *, sha: str, mode: str, source_run: str) -> bool:
    response = api(repo, f"actions/workflows/{EDITORIAL_WORKFLOW}/runs"
                   f"?branch=main&event=workflow_dispatch&head_sha={sha}&per_page=100")
    return any(
        str(run["id"]) != source_run and run["head_sha"] == sha
        and run["head_branch"] == "main" and run["event"] == "workflow_dispatch"
        and run["display_title"] == RETRY_TITLES[mode] and run["status"] in ACTIVE
        for run in response["workflow_runs"]
    )


def retry_inputs(mode: str, *, blob: str, cycle_id: str) -> list[str]:
    if mode == "fallback":
        if not re.fullmatch(r"[a-f0-9]{64}", blob):
            raise ValueError("invalid fallback result digest")
        return ["-f", f"fallback_blob_sha256={blob}"]
    if mode == "fallback-preview" and cycle_id:
        if not re.fullmatch(r"[1-9][0-9]{0,19}", cycle_id):
            raise ValueError("invalid fallback scheduler run")
        return ["-f", f"fallback_cycle_id={cycle_id}"]
    return []


def restart(
    repo: str, *, mode: str, source_run: str, blob: str = "", cycle_id: str = "",
) -> str:
    if mode not in RETRY_MODES or not re.fullmatch(r"[1-9][0-9]{0,19}", source_run):
        raise ValueError("invalid editorial retry")
    preserved = retry_inputs(mode, blob=blob, cycle_id=cycle_id)
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        sha = main_sha(repo)
        status = ci_status(repo, sha)
        if status == "ready":
            # A different GPT payload with the same run title is not a replacement.
            # Its own workflow concurrency key coalesces only the same scope.
            queued = (mode in RETRY_TITLES
                      and already_scheduled(repo, sha=sha, mode=mode, source_run=source_run))
            if main_sha(repo) == sha and time.monotonic() < deadline:
                if queued:
                    return "already_scheduled"
                subprocess.run([
                    "gh", "workflow", "run", EDITORIAL_WORKFLOW, "-R", repo, "--ref", "main",
                    "-f", f"mode={mode}", "-f", "preflight_retry=true", *preserved,
                ], check=True, timeout=20)
                return "dispatched"
        elif status == "ci_failed" and main_sha(repo) == sha:
            return "ci_failed"
        if (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(15, remaining))
    return "ci_pending"


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    mode = os.environ["WORKER_MODE"]
    blob = os.environ.get("GPT_FALLBACK_BLOB_SHA256", "")
    cycle_id = os.environ.get("GPT_FALLBACK_CYCLE_ID", "")
    if sys.argv[1:] == ["gate"]:
        result = start_status(repo, os.environ["GITHUB_SHA"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
            stream.write(f"status={result}\n")
        print(f"editorial_start={result}")
        if result == "ready":
            return 0
        if (
            result in {"superseded", "ci_pending"} and mode in RETRY_MODES
            and os.environ.get("PREFLIGHT_RETRY_ENABLED") == "true"
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
            and os.environ.get("PREFLIGHT_RETRY") != "true"
        ):
            try:
                retry_inputs(mode, blob=blob, cycle_id=cycle_id)
            except ValueError:
                return 1
            return 0
        return 1
    if sys.argv[1:] == ["retry"]:
        outcome = restart(
            repo, mode=mode, source_run=os.environ["GITHUB_RUN_ID"], blob=blob, cycle_id=cycle_id,
        )
        print(f"editorial_start_retry={outcome} mode={mode}")
        return 0 if outcome in {"dispatched", "already_scheduled"} else 1
    raise ValueError("invalid editorial start command")


if __name__ == "__main__":
    raise SystemExit(main())
