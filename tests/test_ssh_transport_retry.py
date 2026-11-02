import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from relocant_media.ops import ssh_state_client as client


@pytest.fixture
def storage():
    return client.SSHStateClient(client.SSHConfig(
        "storage.example", "editorial_state", 22, Path("test-key"), Path("known-hosts")))


@pytest.mark.parametrize("operation", ["health", "read_state", "get_blob", "put_blob"])
def test_safe_transport_retries_exact_request_and_payload(storage, monkeypatch, operation):
    calls = []
    pauses = []
    request = {"op": operation, "sha256": "a" * 64, "size": 7}
    payload = b"payload"

    def run(command, **kwargs):
        calls.append(kwargs["input"])
        if len(calls) < 3:
            raise OSError("private transport failure")
        kwargs["stdout"].write(b'{"ok":true}\n')
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(client.subprocess, "run", run)
    monkeypatch.setattr(client.time, "sleep", pauses.append)
    assert storage.exchange(request, payload) == ({"ok": True}, b"")
    expected = json.dumps(request, separators=(",", ":")).encode() + b"\n" + payload
    assert calls == [expected] * 3
    assert pauses == [1, 2]


def test_transport_storage_error_stops_after_three_attempts(storage, monkeypatch):
    calls = []
    pauses = []

    def once(request, payload=b""):
        calls.append((request.copy(), payload))
        raise client.SSHStorageError("storage_error")

    monkeypatch.setattr(storage, "exchange_once", once)
    monkeypatch.setattr(client.time, "sleep", pauses.append)
    with pytest.raises(client.SSHStorageError, match="storage_error"):
        storage.exchange({"op": "get_blob", "blob_id": "a" * 64})
    assert len(calls) == 3 and pauses == [1, 2]


@pytest.mark.parametrize("code", sorted(client.ERRORS - {"storage_error"}))
def test_permanent_storage_errors_are_not_retried(storage, monkeypatch, code):
    calls = []

    def once(request, payload=b""):
        calls.append(request)
        raise client.SSHStorageError(code)

    monkeypatch.setattr(storage, "exchange_once", once)
    monkeypatch.setattr(client.time, "sleep", lambda delay: pytest.fail("unexpected backoff"))
    with pytest.raises(client.SSHStorageError) as error:
        storage.exchange({"op": "get_blob", "blob_id": "a" * 64})
    assert error.value.code == code and len(calls) == 1


@pytest.mark.parametrize("operation", ["write_state", "create_state"])
def test_pointer_operations_remain_single_shot(storage, monkeypatch, operation):
    calls = []

    def once(request, payload=b""):
        calls.append((request, payload))
        raise client.SSHStorageError("storage_error")

    monkeypatch.setattr(storage, "exchange_once", once)
    monkeypatch.setattr(client.time, "sleep", lambda delay: pytest.fail("pointer retried"))
    with pytest.raises(client.SSHStorageError):
        if operation == "write_state":
            storage.write({"status": "busy", "attempt": "test-attempt"}, "a" * 64)
        else:
            storage.create({"status": "ready", "generation": "test-generation"})
    assert len(calls) == 1 and calls[0][0]["op"] == operation


def test_put_blob_lost_ack_retries_same_content_then_verifies_readback(
    storage, tmp_path, monkeypatch,
):
    path = tmp_path / "blob"
    content = b"immutable snapshot bytes"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    blobs = {}
    puts = []
    pauses = []

    def run(command, **kwargs):
        raw_header, _, payload = kwargs["input"].partition(b"\n")
        request = json.loads(raw_header)
        if request["op"] == "put_blob":
            puts.append(kwargs["input"])
            assert request == {"op": "put_blob", "sha256": digest, "size": len(content)}
            assert hashlib.sha256(payload).hexdigest() == digest
            if digest in blobs:
                assert blobs[digest] == payload
            blobs[digest] = payload
            if len(puts) == 1:
                # The server stored the immutable blob but the reply was lost.
                raise OSError("lost acknowledgement")
            response = {"blob_id": digest}
            body = b""
        else:
            assert request == {"op": "get_blob", "blob_id": digest}
            response = {"sha256": digest, "size": len(content)}
            body = blobs[digest]
        kwargs["stdout"].write(json.dumps(response).encode() + b"\n" + body)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(client.subprocess, "run", run)
    monkeypatch.setattr(client.time, "sleep", pauses.append)
    assert storage.upload(path, "ssh_blobs") == digest
    assert puts[0] == puts[1] and len(puts) == 2
    assert blobs == {digest: content} and pauses == [1]
