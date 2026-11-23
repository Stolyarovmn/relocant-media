import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_actions_runtime import context as _context
from test_actions_runtime import state as _state

from relocant_media.ai.package import canonical_json
from relocant_media.ops import actions_runtime as runtime

context = _context
state = _state


class PrivateStore(runtime.SSHStateClient):
    def __init__(self, files):
        self.files = dict(files)

    def download(self, file_id):
        return self.files[file_id]

    def save(self, data):
        blob_id = hashlib.sha256(data).hexdigest()
        self.files[blob_id] = data
        return blob_id

    def upload(self, path, folder):
        return self.save(path.read_bytes())

    def checkpoint(self, ready, busy_sha, path):
        assert ready["status"] == "ready"
        return "a" * 64


def environment():
    return {"ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
            "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "EDITORIAL_STORAGE_BACKEND": "ssh", "GEMINI_API_KEY": "unused-gemini",
            "EDITORIAL_OWNER_CHAT_ID": "unused-owner", "TELEGRAM_BOT_TOKEN": "mock-bot"}


def test_export_only_writes_private_request_blob(context, tmp_path, monkeypatch, capsys):
    from relocant_media.services import gpt_fallback

    store, old = context
    storage = PrivateStore(old.files)
    request = {"version": 1, "candidates": [{"body": "PRIVATE-DO-NOT-LOG"}]}
    monkeypatch.setattr(gpt_fallback, "build_request", lambda *args, **kwargs: request)
    monkeypatch.setattr(runtime, "run_child", lambda *args: pytest.fail("unexpected worker"))
    monkeypatch.setattr(runtime, "export_public", lambda *args: pytest.fail("unexpected Pages"))
    public = tmp_path / "data.json"
    code = runtime.execute(store, storage, work=tmp_path / "private", output=public,
                           folder="ssh_blobs", mode="fallback-preview", environment=environment())
    assert code == 0
    stdout = capsys.readouterr().out
    assert "PRIVATE-DO-NOT-LOG" not in stdout
    receipt = json.loads(stdout)
    stored = storage.download(receipt["gpt_fallback_request_sha256"])
    assert stored == canonical_json(request).encode()
    assert store.state["status"] == "ready"
    assert store.sha == "initial"
    assert not public.exists()


@pytest.mark.parametrize("schema_failure", [False, True])
def test_preview_skips_invalid_ranked_handoff_before_blob_read_and_keeps_other_work(
    context, tmp_path, monkeypatch, capsys, schema_failure,
):
    from relocant_media.scheduler.models import SchedulerRun
    from relocant_media.services import gpt_fallback

    store, old = context
    storage = PrivateStore(old.files)
    now = datetime.now(UTC)
    finished = now-timedelta(minutes=1 if schema_failure else 31)
    invalid = SchedulerRun(id=123, task_key="editorial-primary:belgrade", city="belgrade",
        task_type="editorial_primary", status="partial", scheduled_for=finished,
        started_at=finished-timedelta(minutes=1), finished_at=finished,
        details={"batch_preselection": {"status": "gpt_ranked", "gpt_slots": 1,
            "gpt_candidate_ids": [1], "full_review_ids": [], "gpt_selected_ids": [1],
            "gpt_rank_sha256": "a"*64, "gpt_request_sha256": "b"*64,
            "gpt_evidence_sha256": {"1": "c"*64},
            "error_type": "ValueError" if schema_failure else "GeminiWorkerError"}})
    monkeypatch.setattr(gpt_fallback, "_recent_runs", lambda *args: [invalid])
    request = {"version": 1, "candidates": [{"body": "PRIVATE-OTHER-WORK"}]}
    monkeypatch.setattr(gpt_fallback, "build_request", lambda *args, **kwargs: request)
    monkeypatch.setattr(runtime, "_gpt_blob",
                        lambda *args: pytest.fail("Истёкший/schema batch не читает blob"))
    assert runtime.execute(store, storage, work=tmp_path / "preview",
        output=tmp_path / "public.json", folder="ssh_blobs", mode="fallback-preview",
        environment=environment()) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert storage.download(receipt["gpt_fallback_request_sha256"]) == canonical_json(
        request).encode()
    assert store.sha == "initial" and store.state["status"] == "ready"


@pytest.mark.parametrize("result_kind", ["package", "rank", "deferred"])
def test_fallback_uses_one_canonical_claim_and_no_primary_model(
    context, tmp_path, monkeypatch, result_kind,
):
    store, old = context
    store.state["last_editorial_at"] = "editorial-before-fallback"
    storage = PrivateStore(old.files)
    request = {"version": 1, "candidates": [{"raw_item_id": 23}]}
    request_sha = storage.save(canonical_json(request).encode())
    result = {"request_sha256": request_sha}
    if result_kind == "rank":
        result.update(version=2, kind="batch_preselection", selected_ids=[23])
    elif result_kind == "deferred":
        result.update(version=1, packages=[], deferred=[
            {"raw_item_id": 23, "reason": "source_confirmation_missing"},
        ])
    else:
        result.update(version=1, packages=[{"raw_item_id": 23}])
    result_sha = storage.save(canonical_json(result).encode())
    seen = []

    def child(command, env):
        assert store.state["status"] == "busy"
        assert command[2] == "relocant_media.ops.gpt_fallback_worker"
        assert json.loads(Path(command[4]).read_text(encoding="utf-8")) == {
            "request": request, "result": result,
        }
        assert "GEMINI_API_KEY" not in env
        assert "EDITORIAL_OWNER_CHAT_ID" not in env
        assert "GPT_FALLBACK_BLOB_SHA256" not in env
        if result_kind != "package":
            assert "TELEGRAM_BOT_TOKEN" not in env
            assert env["EDITORIAL_SEND_ENABLED"] == "false"
        else:
            assert env["TELEGRAM_BOT_TOKEN"] == "mock-bot"
        seen.append(True)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    monkeypatch.setattr(runtime, "export_public", lambda *args: None)
    code = runtime.execute(store, storage, work=tmp_path / "private",
                           output=tmp_path / "data.json", folder="ssh_blobs", mode="fallback",
                           environment={**environment(), "GPT_FALLBACK_BLOB_SHA256": result_sha})
    assert code == 0
    assert seen == [True]
    assert store.state["status"] == "ready"
    assert store.state["last_editorial_at"] == "editorial-before-fallback"


def test_tampered_gpt_blob_fails_before_claim(context, tmp_path):
    store, old = context
    storage = PrivateStore(old.files)
    sha = storage.save(b'{"version":1}')
    storage.files[sha] = b'{"version":2}'
    with pytest.raises(ValueError, match="invalid private GPT exchange blob"):
        runtime.execute(store, storage, work=tmp_path / "private", output=tmp_path / "data.json",
                        folder="ssh_blobs", mode="fallback",
                        environment={**environment(), "GPT_FALLBACK_BLOB_SHA256": sha})
    assert store.sha == "initial"


def test_evidence_refresh_exports_private_request_after_state_commit(
    context, tmp_path, monkeypatch, capsys,
):
    store, old = context
    storage = PrivateStore(old.files)
    request = {"version": 1, "candidates": [{"raw_item_id": 23}]}
    request_sha = storage.save(canonical_json(request).encode())
    result = {
        "version": 3, "kind": "evidence_refresh", "request_sha256": request_sha,
        "raw_item_id": 23, "urls": ["https://bgdmarathon.org/example"],
    }
    result_sha = storage.save(canonical_json(result).encode())
    refreshed = {"version": 1, "candidates": [{"raw_item_id": 23, "body": "PRIVATE-DO-NOT-LOG"}]}
    original_upload = storage.upload

    def upload(path, folder):
        if folder == "gpt-fallback-request":
            assert store.state["status"] == "ready"
        return original_upload(path, folder)

    def child(command, env):
        assert store.state["status"] == "busy"
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert env["EDITORIAL_SEND_ENABLED"] == "false"
        assert json.loads(Path(command[4]).read_text(encoding="utf-8")) == {
            "request": request, "result": result,
        }
        directory = Path(command[6])
        directory.mkdir()
        (directory / "gpt-refreshed-request.json").write_text(
            canonical_json(refreshed), encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(storage, "upload", upload)
    monkeypatch.setattr(runtime, "run_child", child)
    monkeypatch.setattr(runtime, "export_public", lambda *args: None)
    code = runtime.execute(store, storage, work=tmp_path / "private",
                           output=tmp_path / "data.json", folder="ssh_blobs", mode="fallback",
                           environment={**environment(), "GPT_FALLBACK_BLOB_SHA256": result_sha})
    assert code == 0
    captured = capsys.readouterr().out
    assert "PRIVATE-DO-NOT-LOG" not in captured
    receipt = next(json.loads(line) for line in captured.splitlines()
                   if line.startswith('{"gpt_fallback_request_sha256"'))
    assert storage.download(receipt["gpt_fallback_request_sha256"]) == canonical_json(
        refreshed,
    ).encode()
