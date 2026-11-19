import hashlib
import json
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


@pytest.mark.parametrize("rank_only", [False, True])
def test_fallback_uses_one_canonical_claim_and_no_primary_model(
    context, tmp_path, monkeypatch, rank_only,
):
    store, old = context
    store.state["last_editorial_at"] = "editorial-before-fallback"
    storage = PrivateStore(old.files)
    request = {"version": 1, "candidates": [{"raw_item_id": 23}]}
    request_sha = storage.save(canonical_json(request).encode())
    result = (
        {"version": 2, "kind": "batch_preselection", "request_sha256": request_sha,
         "selected_ids": [23]}
        if rank_only else
        {"version": 1, "request_sha256": request_sha, "packages": [{"raw_item_id": 23}]}
    )
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
        if rank_only:
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
