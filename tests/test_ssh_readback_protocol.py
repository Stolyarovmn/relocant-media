import hashlib

import pytest

from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient, SSHStorageError
from relocant_media.ops.ssh_state_client import _state_digest as state_digest


@pytest.fixture
def blob(tmp_path):
    path = tmp_path / "snapshot"
    content = b"private test snapshot"
    path.write_bytes(content)
    config = SSHConfig("storage.example", "editorial_state", 22, path, path)
    return SSHStateClient(config), path, content, hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("capability", [None, "unknown", "sha256-v1"])
def test_upload_selects_verified_remote_readback_or_compatible_download(
    blob, monkeypatch, capability,
):
    client, path, content, digest = blob
    calls = []
    state = {"generation": "fixture"}

    def exchange(request, payload=b""):
        operation = request["op"]
        calls.append(operation)
        if operation == "read_state":
            return {"state": state, "sha": state_digest(state), "blob_readback": capability}, b""
        if operation == "put_blob":
            assert payload == content and request["sha256"] == digest
            return {"blob_id": digest}, b""
        assert request["blob_id"] == digest and not payload
        return {"sha256": digest, "size": len(content)}, content if operation == "get_blob" else b""

    monkeypatch.setattr(client, "exchange", exchange)
    client.read()
    assert client.upload(path, "fixture") == digest
    expected = "verify_blob" if capability == "sha256-v1" else "get_blob"
    assert calls == ["read_state", "put_blob", expected]


def test_explicit_endpoint_rollback_restores_full_download_for_same_client(blob, monkeypatch):
    client, path, content, digest = blob
    calls = []
    state = {"generation": "fixture"}

    def exchange(request, payload=b""):
        operation = request["op"]
        calls.append(operation)
        if operation == "read_state":
            return {"state": state, "sha": state_digest(state), "blob_readback": "sha256-v1"}, b""
        if operation == "put_blob":
            return {"blob_id": digest}, b""
        if operation == "verify_blob":
            raise SSHStorageError("invalid_request")
        assert operation == "get_blob"
        return {"sha256": digest, "size": len(content)}, content

    monkeypatch.setattr(client, "exchange", exchange)
    client.read()
    assert client.upload(path, "fixture") == client.upload(path, "fixture") == digest
    assert calls == ["read_state", "put_blob", "verify_blob", "get_blob", "put_blob", "get_blob"]


@pytest.mark.parametrize("failure", ["digest", "size", "body", "storage_error"])
def test_bad_readback_blocks_upload_without_falling_back(blob, monkeypatch, failure):
    client, path, content, digest = blob
    state = {"generation": "fixture"}

    def exchange(request, payload=b""):
        operation = request["op"]
        if operation == "read_state":
            return {"state": state, "sha": state_digest(state), "blob_readback": "sha256-v1"}, b""
        if operation == "put_blob":
            return {"blob_id": digest}, b""
        assert operation == "verify_blob"
        if failure == "storage_error":
            raise SSHStorageError("storage_error")
        value = {"sha256": digest, "size": len(content)}
        if failure == "digest":
            value["sha256"] = "0" * 64
        if failure == "size":
            value["size"] = len(content) + 1
        return value, b"unexpected" if failure == "body" else b""

    monkeypatch.setattr(client, "exchange", exchange)
    client.read()
    with pytest.raises(SSHStorageError, match="storage_error"):
        client.upload(path, "fixture")
