import json
from datetime import UTC, datetime

import pytest

from relocant_media.ops import gpt_submission as receipt

DIGEST = "a" * 64
HEAD = "b" * 40


@pytest.mark.parametrize("changed", [
    None, "digest", "mode", "path", "branch", "attempt", "head", "event", "future_shape",
])
def test_submission_time_is_from_the_matching_workflow_dispatch(tmp_path, monkeypatch, changed):
    event = tmp_path / "event.json"
    inputs = {"mode": "fallback", "fallback_blob_sha256": DIGEST}
    environment = {
        "GITHUB_REPOSITORY": receipt.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": HEAD, "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_PATH": str(event),
    }
    run = {
        "id": 42, "run_attempt": 1, "event": "workflow_dispatch", "head_branch": "main",
        "head_sha": HEAD, "status": "in_progress",
        "path": ".github/workflows/editorial-actions.yml",
        "created_at": "2026-11-24T13:58:32Z",
    }
    if changed == "digest":
        inputs["fallback_blob_sha256"] = "c" * 64
    elif changed == "mode":
        inputs["mode"] = "editorial"
    elif changed == "future_shape":
        run["created_at"] = "2099-11-24T13:58:32"
    elif changed is not None:
        key = {"branch": "head_branch", "head": "head_sha", "attempt": "run_attempt"}.get(
            changed, changed,
        )
        run[key] = "changed"
    event.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")

    def api(repo, path):
        assert repo == receipt.REPOSITORY and path == "actions/runs/42"
        return run

    monkeypatch.setattr(receipt, "api", api)
    observed = receipt.verified_submission(environment, DIGEST)
    if changed is not None:
        assert observed is None
        return
    assert observed == receipt.GPTSubmission(
        42, DIGEST, datetime(2026, 11, 24, 13, 58, 32, tzinfo=UTC),
    )
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(observed.audit()), encoding="utf-8")
    assert receipt.GPTSubmission.read(path) == observed
