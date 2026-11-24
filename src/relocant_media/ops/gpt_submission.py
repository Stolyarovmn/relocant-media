"""Подтверждённое GitHub время постановки конкретного ответа в очередь."""

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relocant_media.ops.editorial_start import EDITORIAL_WORKFLOW, api

REPOSITORY = "Stolyarovmn/relocant-media"
_RUN_ID = re.compile(r"[1-9][0-9]{0,19}")
_WORKFLOW_PATH = f".github/workflows/{EDITORIAL_WORKFLOW}"
_API_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError)


@dataclass(frozen=True)
class GPTSubmission:
    run_id: int
    result_sha256: str
    received_at: datetime
    retry_run_id: int | None = None

    def audit(self) -> dict[str, Any]:
        value = {
            "run_id": self.run_id,
            "result_sha256": self.result_sha256,
            "received_at": self.received_at.isoformat(),
        }
        if self.retry_run_id is not None:
            value["retry_run_id"] = self.retry_run_id
        return value

    @classmethod
    def read(cls, path: Path) -> "GPTSubmission":
        value = json.loads(path.read_text(encoding="utf-8"))
        received = datetime.fromisoformat(value["received_at"])
        retry = value.get("retry_run_id")
        if (type(value["run_id"]) is not int or value["run_id"] <= 0
                or not re.fullmatch(r"[a-f0-9]{64}", value["result_sha256"])
                or (retry is not None and
                    (type(retry) is not int or retry <= 0 or retry == value["run_id"]))
                or received.utcoffset() is None):
            raise ValueError("invalid fallback submission receipt")
        return cls(value["run_id"], value["result_sha256"], received.astimezone(UTC), retry)


def _retry_origin(source: int, current: GPTSubmission) -> GPTSubmission | None:
    """Читаем первую попытку запуска, который остановился до рабочего состояния."""
    path = f"actions/runs/{source}/attempts/1"
    run = api(REPOSITORY, path)
    if (run["id"] != source or run["run_attempt"] != 1
            or run["path"] != _WORKFLOW_PATH or run["event"] != "workflow_dispatch"
            or run["head_branch"] != "main" or run["head_repository"]["full_name"] != REPOSITORY):
        return None
    received = datetime.fromisoformat(run["created_at"])
    if received.utcoffset() is None or received > current.received_at:
        return None

    jobs = api(REPOSITORY, f"{path}/jobs?per_page=30")["jobs"]
    cycles = [job for job in jobs if job["name"] == "cycle"]
    if len(cycles) != 1:
        return None
    cycle = cycles[0]
    if (cycle["run_id"] != source or cycle["run_attempt"] != 1
            or cycle["head_sha"] != run["head_sha"] or cycle["conclusion"] != "success"
            or cycle["status"] != "completed"):
        return None
    steps = {(step["name"], step["status"], step["conclusion"]) for step in cycle["steps"]}
    required = {
        (f"GPT fallback preflight receipt {current.result_sha256}", "completed", "success"),
        ("Prepare storage-only SSH identity", "completed", "skipped"),
        ("Restore, claim, process, save and export", "completed", "skipped"),
    }
    if not required <= steps:
        return None
    return GPTSubmission(source, current.result_sha256, received.astimezone(UTC), current.run_id)


def verified_submission(environment: Mapping[str, str], digest: str) -> GPTSubmission | None:
    """Время из Actions API, прямая привязка текущего события к хешу ответа."""
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    head = environment.get("GITHUB_SHA", "")
    if (environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environment.get("GITHUB_REF") != "refs/heads/main"
            or not _RUN_ID.fullmatch(run_id)
            or not re.fullmatch(r"[1-9][0-9]{0,3}", attempt)
            or not re.fullmatch(r"[a-f0-9]{40}", head)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        return None
    try:
        event = json.loads(Path(environment["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        inputs = event["inputs"]
        if inputs.get("mode") != "fallback" or inputs.get("fallback_blob_sha256") != digest:
            return None
        run = api(REPOSITORY, f"actions/runs/{run_id}")
        if (run["id"] != int(run_id) or run["run_attempt"] != int(attempt)
                or run["event"] != "workflow_dispatch" or run["head_branch"] != "main"
                or run["head_sha"] != head or run["status"] != "in_progress"
                or run["path"] != _WORKFLOW_PATH):
            return None
        received = datetime.fromisoformat(run["created_at"])
        if received.utcoffset() is None:
            return None
        current = GPTSubmission(int(run_id), digest, received.astimezone(UTC))
    except _API_ERRORS:
        return None
    source = inputs.get("fallback_retry_source_run")
    if (attempt != "1" or inputs.get("preflight_retry") not in ("true", True)
            or not isinstance(source, str) or not _RUN_ID.fullmatch(source) or source == run_id):
        return current
    try:
        if (run["actor"]["login"] != "github-actions[bot]"
                or run["triggering_actor"]["login"] != "github-actions[bot]"):
            return current
        return _retry_origin(int(source), current) or current
    except _API_ERRORS:
        return current
