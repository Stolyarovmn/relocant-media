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


@pytest.mark.parametrize("changed", [
    None, "digest", "runtime", "storage", "actor", "trigger", "manual", "repeat", "path", "attempt",
    "source_repo", "source_branch", "source_attempt", "future", "cycle_head", "api",
])
def test_automatic_retry_keeps_only_the_matching_preflight_receipt(tmp_path, monkeypatch, changed):
    event = tmp_path / "event.json"
    inputs = {
        "mode": "fallback", "fallback_blob_sha256": DIGEST,
        "preflight_retry": "true", "fallback_retry_source_run": "40",
    }
    environment = {
        "GITHUB_REPOSITORY": receipt.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": HEAD, "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_PATH": str(event),
    }
    original_head = "e" * 40
    run = {
        "id": 42, "run_attempt": 1, "event": "workflow_dispatch", "head_branch": "main",
        "head_sha": HEAD, "status": "in_progress",
        "path": ".github/workflows/editorial-actions.yml",
        "created_at": "2026-11-24T14:03:18Z", "actor": {"login": "github-actions[bot]"},
        "triggering_actor": {"login": "github-actions[bot]"},
    }
    origin = {
        **run, "id": 40, "head_sha": original_head, "status": "completed",
        "head_repository": {"full_name": receipt.REPOSITORY}, "created_at": "2026-11-24T13:58:32Z",
    }
    steps = [
        {"name": f"GPT fallback preflight receipt {DIGEST}", "status": "completed",
         "conclusion": "success"},
        {"name": "Prepare storage-only SSH identity", "status": "completed",
         "conclusion": "skipped"},
        {"name": "Restore, claim, process, save and export", "status": "completed",
         "conclusion": "skipped"},
    ]
    job = {
        "name": "cycle", "run_id": 40, "run_attempt": 1, "head_sha": original_head,
        "status": "completed", "conclusion": "success", "steps": steps,
    }
    if changed == "digest":
        steps[0]["name"] = f"GPT fallback preflight receipt {'c' * 64}"
    elif changed == "storage":
        steps[1]["conclusion"] = "success"
    elif changed == "runtime":
        steps[2]["conclusion"] = "success"
    elif changed == "actor":
        run["actor"] = {"login": "manual-user"}
    elif changed == "trigger":
        run["triggering_actor"] = {"login": "manual-user"}
    elif changed == "manual":
        inputs["preflight_retry"] = "false"
    elif changed == "repeat":
        inputs["fallback_retry_source_run"] = "42"
    elif changed == "attempt":
        environment["GITHUB_RUN_ATTEMPT"] = "2"
        run["run_attempt"] = 2
    elif changed == "source_repo":
        origin["head_repository"] = {"full_name": "other/repo"}
    elif changed == "source_branch":
        origin["head_branch"] = "other"
    elif changed == "source_attempt":
        origin["run_attempt"] = 2
    elif changed == "future":
        origin["created_at"] = "2026-11-24T14:03:19Z"
    elif changed == "path":
        origin["path"] = ".github/workflows/other.yml"
    elif changed == "cycle_head":
        job["head_sha"] = HEAD
    event.write_text(json.dumps({"inputs": inputs}), encoding="utf-8")
    requested = []

    def api(repo, path):
        assert repo == receipt.REPOSITORY
        requested.append(path)
        if changed == "api" and path.endswith("/jobs?per_page=30"):
            raise OSError("workflow jobs temporarily unavailable")
        return {
            "actions/runs/42": run,
            "actions/runs/40/attempts/1": origin,
            "actions/runs/40/attempts/1/jobs?per_page=30": {"jobs": [job]},
        }[path]

    monkeypatch.setattr(receipt, "api", api)
    observed = receipt.verified_submission(environment, DIGEST)
    if changed in {"actor", "trigger", "manual", "repeat", "attempt"}:
        assert requested == ["actions/runs/42"]
    if changed is not None:
        assert observed == receipt.GPTSubmission(
            42, DIGEST, datetime(2026, 11, 24, 14, 3, 18, tzinfo=UTC),
        )
        return
    assert observed == receipt.GPTSubmission(
        40, DIGEST, datetime(2026, 11, 24, 13, 58, 32, tzinfo=UTC), retry_run_id=42,
    )
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(observed.audit()), encoding="utf-8")
    assert receipt.GPTSubmission.read(path) == observed
