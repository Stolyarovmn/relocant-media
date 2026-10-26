import base64
import json
from pathlib import Path

import httpx
import pytest
import yaml

from relocant_media.ops.newsroom_attempt import fetch_previous, record_attempt
from relocant_media.ops.newsroom_export import snapshot
from relocant_media.ops.newsroom_merge import merge

ROOT = Path(__file__).resolve().parents[1]
OLD = "2026-10-24T09:24:05+00:00"


def projection():
    result = snapshot(ROOT)
    result["cities"][0].update(runtime_status="available", observed_at=OLD,
                                counts={"sent": 17}, items=[], total_events=17)
    return result


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
def test_failed_attempt_keeps_only_previous_observation(tmp_path, outcome):
    previous = tmp_path / "previous.json"
    prior = projection()
    prior["private"] = "SECRET"
    prior["cities"][0]["private"] = "SECRET"
    previous.write_text(json.dumps(prior))
    # No access to a possibly modified working SQLite or private run reports.
    (tmp_path / "working.sqlite3").write_bytes(b"PRIVATE_DATABASE")
    output = tmp_path / "data.json"
    result = record_attempt(ROOT, output, previous, outcome=outcome,
                            mode="intake", run_id="123", run_attempt="2")
    city = result["cities"][0]
    assert city["observed_at"] == OLD and city["counts"] == {"sent": 17}
    assert result["cities"][1]["observed_at"] is None
    assert result["last_attempt"]["projection"] == "previous_or_unavailable"
    assert result["last_attempt"]["status"] == outcome
    assert "SECRET" not in output.read_text() and "PRIVATE_DATABASE" not in output.read_text()
    # Publishing and the separate Pages build reconstruct through this merge.
    assert merge(snapshot(ROOT), result)["last_attempt"] == result["last_attempt"]


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_checkpointed_projection_wins_even_for_nonzero_worker_exit(tmp_path, outcome):
    output, previous = tmp_path / "data.json", tmp_path / "previous.json"
    current = projection()
    current["cities"][0]["counts"] = {"sent": 18}
    output.write_text(json.dumps(current))
    previous.write_text(json.dumps(projection()))
    result = record_attempt(ROOT, output, previous, outcome=outcome,
                            mode="editorial", run_id="123", run_attempt="1")
    assert result["cities"][0]["counts"] == {"sent": 18}
    assert result["last_attempt"]["projection"] == "verified"
    assert result["last_attempt"]["status"] == outcome


def test_no_previous_snapshot_remains_unknown(tmp_path):
    result = record_attempt(ROOT, tmp_path / "data.json", tmp_path / "absent.json",
                            outcome="skipped", mode="not_selected", run_id="123", run_attempt="1")
    assert all(city["observed_at"] is None and city["counts"] is None
               for city in result["cities"])


def test_attempt_merge_drops_private_payload():
    payload = projection()
    payload["last_attempt"] = {"status": "failure", "exception": "SECRET"}
    assert "SECRET" not in json.dumps(merge(snapshot(ROOT), payload))


@pytest.mark.parametrize("field,value", [
    ("mode", "PRIVATE_TEXT"), ("outcome", "PRIVATE_EXCEPTION"), ("run_id", "token=SECRET"),
])
def test_attempt_rejects_unbounded_metadata(tmp_path, field, value):
    options = {"outcome": "failure", "mode": "preview", "run_id": "123", "run_attempt": "1"}
    options[field] = value
    with pytest.raises(ValueError):
        record_attempt(ROOT, tmp_path / "data.json", tmp_path / "absent", **options)
    assert not (tmp_path / "data.json").exists()


def test_runtime_workflow_observes_failures_without_broadening_execution_rights():
    workflow = yaml.safe_load((ROOT / ".github/workflows/editorial-actions.yml").read_text())
    cycle = workflow["jobs"]["cycle"]
    assert cycle["permissions"] == {"contents": "write", "actions": "read"}
    steps = cycle["steps"]
    runtime = next(step for step in steps if step.get("id") == "runtime")
    observation = next(step for step in steps if step.get("id") == "projection")
    assert observation["if"] == "always()"
    assert observation["env"]["RUNTIME_OUTCOME"] == "${{ steps.runtime.outcome }}"
    assert "newsroom_attempt" in observation["run"]
    assert "--fetch-previous" in observation["run"]
    assert "DATABASE_URL" not in observation["env"]
    assert "continue-on-error" not in runtime


@pytest.mark.parametrize("status", [200, 404, 403, 500])
def test_previous_read_distinguishes_absent_from_unavailable(tmp_path, monkeypatch, status):
    destination = tmp_path / "previous.json"
    destination.write_text("stale local file")
    content = json.dumps(projection()).encode()
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json={
            "encoding": "base64", "content": base64.b64encode(content).decode(),
        })

    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(handle)))
    if status in {403, 500}:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_previous(destination, "test-only-token")
        assert not destination.exists()
    else:
        fetch_previous(destination, "test-only-token")
        assert destination.exists() == (status == 200)
        if status == 200:
            assert destination.read_bytes() == content
    assert requests[0].headers["Authorization"] == "Bearer test-only-token"
    assert requests[0].url.params["ref"] == "newsroom-data"
