import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from relocant_media.ops import actions_bootstrap, actions_runtime
from relocant_media.ops import ssh_state_client as client
from relocant_media.ops import ssh_state_storage as helper
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def environment(tmp_path):
    key = tmp_path / "storage-key"
    key.write_text("PRIVATE_TEST_KEY")
    key.chmod(0o600)
    hosts = tmp_path / "storage-known-hosts"
    hosts.write_text("storage.example ssh-ed25519 PUBLIC_TEST_HOST_KEY")
    hosts.chmod(0o600)
    return {"EDITORIAL_STORAGE_BACKEND": "ssh", "EDITORIAL_SSH_HOST": "storage.example",
            "EDITORIAL_SSH_USER": "editorial_state", "EDITORIAL_SSH_PORT": "2222",
            "EDITORIAL_SSH_KEY_PATH": str(key), "EDITORIAL_SSH_KNOWN_HOSTS_PATH": str(hosts),
            "ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": actions_runtime.REPO,
            "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "1234"}


@pytest.fixture
def backend(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir(mode=0o700)
    marker = tmp_path / "cutover.json"
    monkeypatch.setattr(helper, "TRUSTED_MARKER_UID", os.geteuid())
    return helper.Storage(root, marker)


def transport(monkeypatch, backend):
    calls = []
    def run(command, *, input, stdout, stderr, timeout, check):
        assert command[0] == "ssh" and command[-1] == client.REMOTE_HELPER
        assert stderr == subprocess.DEVNULL and timeout == 180 and check is False
        assert "-oStrictHostKeyChecking=yes" in command and "-oBatchMode=yes" in command
        assert "-F" in command and "/dev/null" in command
        assert "PRIVATE_TEST_KEY" not in " ".join(command)
        calls.append(json.loads(input.partition(b"\n")[0])["op"])
        try:
            helper.serve(backend, io.BytesIO(input), stdout)
            code = 0
        except helper.StorageError as error:
            stdout.write(json.dumps({"error": error.code}).encode() + b"\n")
            code = 1
        return subprocess.CompletedProcess(command, code)
    monkeypatch.setattr(client.subprocess, "run", run)
    return calls


def final_snapshot(tmp_path):
    database = tmp_path / "canonical.sqlite3"
    initialize_sqlite(database)
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO telegram_update_receipt(update_id,kind,status) "
                                "VALUES(12345,'message','processed')"))
    engine.dispose()
    manifest = create_snapshot(database, tmp_path / "final")
    return manifest, tmp_path / "final/editorial.sqlite3"


def test_transport_is_pinned_noninteractive_and_refuses_dangerous_configuration(environment):
    config = client.SSHConfig.from_environment(environment)
    command = config.command()
    assert command[-2] == "editorial_state@storage.example"
    assert command[-1] == client.REMOTE_HELPER
    assert "-oIdentityAgent=none" in command
    assert "-oForwardAgent=no" in command
    assert "-oClearAllForwardings=yes" in command
    assert "-oPasswordAuthentication=no" in command


@pytest.mark.parametrize("field,value", [
    ("EDITORIAL_SSH_HOST", "-oProxyCommand=evil"), ("EDITORIAL_SSH_HOST", "host;echo token"),
    ("EDITORIAL_SSH_HOST", "host/path"), ("EDITORIAL_SSH_USER", "root"),
    ("EDITORIAL_SSH_USER", "user@host"), ("EDITORIAL_SSH_PORT", "22 -o foo"),
    ("EDITORIAL_SSH_PORT", "0"), ("EDITORIAL_SSH_PORT", "65536"),
    ("EDITORIAL_SSH_KEY_PATH", "/tmp/key%h"),
])
def test_invalid_ssh_inputs_never_run_subprocess(environment, monkeypatch, field, value):
    environment[field] = value
    monkeypatch.setattr(client.subprocess, "run", lambda *a, **k: pytest.fail("network"))
    with pytest.raises(ValueError):
        client.SSHConfig.from_environment(environment)


def test_private_key_permissions_and_symlinks_are_rejected(environment, tmp_path):
    key = Path(environment["EDITORIAL_SSH_KEY_PATH"])
    key.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        client.SSHConfig.from_environment(environment)
    key.chmod(0o600)
    link = tmp_path / "alias"
    link.symlink_to(key)
    environment["EDITORIAL_SSH_KEY_PATH"] = str(link)
    with pytest.raises(ValueError, match="existing"):
        client.SSHConfig.from_environment(environment)


def test_blob_roundtrip_state_cas_and_busy_replay_block_without_github(
    environment, backend, tmp_path, monkeypatch,
):
    calls = transport(monkeypatch, backend)
    manifest, database = final_snapshot(tmp_path)
    backend.marker.write_text(json.dumps({"version": 1, "server_writers_fenced": True,
        "snapshot_sha256": manifest["sha256"], "snapshot_bytes": manifest["bytes"]}))
    backend.marker.chmod(0o644)
    storage = client.SSHStateClient(client.SSHConfig.from_environment(environment))
    assert storage.health() == {"storage_ready": True, "bootstrapped": False}
    storage.require_empty()
    manifest["drive_file_id"] = storage.upload(database, "ssh_blobs")
    state = actions_runtime.bootstrap_record(manifest, now=actions_runtime.datetime.now(
        actions_runtime.UTC))
    state["intake_state"] = {"signature": "private-offset", "attempts": 2}
    storage.create(state)
    assert storage.health() == {"storage_ready": True, "bootstrapped": True}
    with pytest.raises(client.SSHStorageError, match="state_exists"):
        storage.require_empty()
    original, sha = storage.read()
    claimed = {**original, "status": "busy", "attempt": "attempt-1"}
    storage.write(claimed, sha)
    with pytest.raises(client.SSHStorageError, match="conflict"):
        storage.write({**claimed, "attempt": "racing-writer"}, sha)
    stored, _ = storage.read()
    assert stored["status"] == "busy" and stored["intake_state"]["attempts"] == 2
    with pytest.raises(ValueError, match="not ready"):
        actions_runtime.execute(storage, storage, work=tmp_path / "runner", output=tmp_path / "out",
                                folder="ssh_blobs", mode="editorial", environment=environment)
    assert "write_state" in calls


def test_runtime_restores_server_blob_executes_only_on_runner_and_keeps_offsets(
    environment, backend, tmp_path, monkeypatch,
):
    transport(monkeypatch, backend)
    manifest, database = final_snapshot(tmp_path)
    backend.marker.write_text(json.dumps({"version": 1, "server_writers_fenced": True,
        "snapshot_sha256": manifest["sha256"], "snapshot_bytes": manifest["bytes"]}))
    backend.marker.chmod(0o644)
    storage = client.SSHStateClient(client.SSHConfig.from_environment(environment))
    manifest["drive_file_id"] = storage.upload(database, "ssh_blobs")
    state = actions_runtime.bootstrap_record(manifest, now=actions_runtime.datetime.now(
        actions_runtime.UTC))
    storage.create(state)
    environment.update(EDITORIAL_SSH_PRIVATE_KEY="PRIVATE_TEST_KEY", SSH_AUTH_SOCK="agent-secret")
    children = []
    def child(command, env):
        assert not any(key.startswith("EDITORIAL_SSH_") for key in env)
        assert "SSH_AUTH_SOCK" not in env and "EDITORIAL_STORAGE_BACKEND" not in env
        assert env["RELOCANT_RUNTIME_PLATFORM"] == "github-actions"
        assert env["DATABASE_URL"].startswith(f"sqlite:///{tmp_path}")
        assert storage.read()[0]["status"] == "busy"
        engine = create_engine(env["DATABASE_URL"])
        with engine.begin() as connection:
            assert connection.execute(text("SELECT update_id FROM telegram_update_receipt")).scalar(
                ) == 12345
            connection.execute(text("INSERT INTO telegram_update_receipt(update_id,kind,status) "
                                    "VALUES(12346,'message','processed')"))
        engine.dispose()
        children.append(command)
        return 0
    monkeypatch.setattr(actions_runtime, "run_child", child)
    monkeypatch.setattr(actions_runtime, "export_public", lambda *args: None)
    assert actions_runtime.execute(storage, storage, work=tmp_path / "runner",
        output=tmp_path / "out", folder="ssh_blobs", mode="editorial", environment=environment) == 0
    committed, _ = storage.read()
    assert committed["status"] == "ready" and len(children) == 1
    assert committed["snapshot"]["drive_file_id"] != state["snapshot"]["drive_file_id"]
    restored = tmp_path / "readback"
    restored.mkdir()
    database = actions_runtime.restored(storage, committed["snapshot"], restored)
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT update_id FROM telegram_update_receipt "
                                       "ORDER BY update_id")).scalars().all() == [12345, 12346]
    engine.dispose()


def test_ssh_bootstrap_uses_fresh_existing_database_and_cannot_replace_pointer(
    environment, backend, tmp_path, monkeypatch,
):
    calls = transport(monkeypatch, backend)
    manifest, database = final_snapshot(tmp_path)
    backend.marker.write_text(json.dumps({"version": 1, "server_writers_fenced": True,
        "snapshot_sha256": manifest["sha256"], "snapshot_bytes": manifest["bytes"]}))
    backend.marker.chmod(0o644)
    final = tmp_path / "manifest.json"
    final.write_text(json.dumps(manifest))
    intake = tmp_path / "intake.json"
    intake.write_text('{"attempts":2,"signature":"existing-offset"}')
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GH_STATE_TOKEN", raising=False)
    monkeypatch.delenv("GH_STATE_REPOSITORY", raising=False)
    args = ["bootstrap", "--final-manifest", str(final), "--final-database", str(database),
            "--intake-state", str(intake), "--work", str(tmp_path / "bootstrap"),
            "--confirm-all-former-writers-fenced"]
    monkeypatch.setattr(sys, "argv", args)
    actions_bootstrap.main()
    record = backend.read_state()["state"]
    assert record["intake_state"] == {"attempts": 2, "signature": "existing-offset"}
    assert record["snapshot"]["drive_file_id"] == manifest["sha256"]
    assert calls.count("create_state") == 1
    count = calls.count("put_blob")
    args[args.index(str(tmp_path / "bootstrap"))] = str(tmp_path / "second-bootstrap")
    with pytest.raises(SystemExit) as error:
        actions_bootstrap.main()
    assert error.value.code == 1 and calls.count("put_blob") == count
    assert backend.read_state()["state"] == record


def test_factory_selects_ssh_without_drive_github_or_credentials(environment, monkeypatch):
    monkeypatch.setattr(actions_runtime, "Drive", lambda *args: pytest.fail("Drive"))
    monkeypatch.setattr(actions_runtime, "StateStore",
                        lambda *args, **kwargs: pytest.fail("GitHub"))
    with httpx.Client() as http:
        store, blobs, folder = actions_runtime.build_storage(http, environment)
        assert store is blobs and isinstance(store, client.SSHStateClient) and folder == "ssh_blobs"
        with pytest.raises(ValueError, match="must be ssh or drive"):
            actions_runtime.build_storage(http, {"EDITORIAL_STORAGE_BACKEND": "other"})


@pytest.mark.parametrize("response", [
    b'{"error":"private exception text with token"}\n', b'{"error":[]}\n',
    b'{"size":3,"sha256":"' + b"a" * 64 + b'"}\nBAD', b'not-json\n',
])
def test_corrupt_or_private_remote_errors_are_sanitized(environment, monkeypatch, response):
    def run(command, **kwargs):
        kwargs["stdout"].write(response)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(client.subprocess, "run", run)
    storage = client.SSHStateClient(client.SSHConfig.from_environment(environment))
    with pytest.raises((client.SSHStorageError, ValueError)) as error:
        storage.download("a" * 64)
    assert "private exception" not in str(error.value) and "token" not in str(error.value)


def test_failed_upload_readback_never_returns_blob_receipt(environment, tmp_path, monkeypatch):
    storage = client.SSHStateClient(client.SSHConfig.from_environment(environment))
    path = tmp_path / "blob"
    path.write_bytes(b"verified bytes")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(storage, "exchange", lambda *args: ({"blob_id": digest}, b""))
    monkeypatch.setattr(storage, "download", lambda *args: b"corrupt readback")
    with pytest.raises(client.SSHStorageError):
        storage.upload(path, "ssh_blobs")
