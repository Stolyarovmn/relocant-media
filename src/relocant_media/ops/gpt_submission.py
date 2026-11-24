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


@dataclass(frozen=True)
class GPTSubmission:
    run_id: int
    result_sha256: str
    received_at: datetime

    def audit(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "result_sha256": self.result_sha256,
            "received_at": self.received_at.isoformat(),
        }

    @classmethod
    def read(cls, path: Path) -> "GPTSubmission":
        value = json.loads(path.read_text(encoding="utf-8"))
        received = datetime.fromisoformat(value["received_at"])
        if (type(value["run_id"]) is not int or value["run_id"] <= 0
                or not re.fullmatch(r"[a-f0-9]{64}", value["result_sha256"])
                or received.utcoffset() is None):
            raise ValueError("invalid fallback submission receipt")
        return cls(value["run_id"], value["result_sha256"], received.astimezone(UTC))


def verified_submission(environment: Mapping[str, str], digest: str) -> GPTSubmission | None:
    """Время из Actions API, прямая привязка текущего события к хешу ответа."""
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    head = environment.get("GITHUB_SHA", "")
    if (environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or environment.get("GITHUB_REF") != "refs/heads/main"
            or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
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
                or run["path"] != f".github/workflows/{EDITORIAL_WORKFLOW}"):
            return None
        received = datetime.fromisoformat(run["created_at"])
        if received.utcoffset() is None:
            return None
        return GPTSubmission(int(run_id), digest, received.astimezone(UTC))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        return None
