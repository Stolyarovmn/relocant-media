import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient


def test_checkpoint_retains_exact_cas_budget_and_exit_independently(tmp_path, monkeypatch):
    client = SSHStateClient(SSHConfig('storage.example', 'state', 22, Path('key'), Path('hosts')))
    blobs = {}
    def upload(path, folder):
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        blobs[digest] = content
        return digest
    monkeypatch.setattr(client, 'upload', upload)
    state = {'status': 'ready', 'snapshot': {'sha256': 'c' * 64},
             'intake_state': {'attempts': 2, 'backup_pending': False}, 'last_exit_code': 1}
    digest = client.checkpoint(state, 'b' * 64, tmp_path / 'checkpoint.json')
    checkpoint = json.loads(blobs[digest])
    assert checkpoint == {'version': 1, 'expected_busy_sha256': 'b' * 64, 'state': state}
    assert hashlib.sha256(blobs[digest]).hexdigest() == digest


def test_checkpoint_rejects_busy_or_unverified_digest_before_upload(tmp_path, monkeypatch):
    client = SSHStateClient(SSHConfig('storage.example', 'state', 22, Path('key'), Path('hosts')))
    monkeypatch.setattr(client, 'upload', lambda *args: pytest.fail('must not upload'))
    with pytest.raises(ValueError):
        client.checkpoint({'status': 'busy'}, 'b' * 64, tmp_path / 'busy.json')
    with pytest.raises(ValueError):
        client.checkpoint({'status': 'ready'}, 'unknown', tmp_path / 'bad.json')


@pytest.fixture
def recovery_fixture(tmp_path):
    from datetime import UTC, datetime

    from relocant_media.ops.actions_runtime import bootstrap_record
    from relocant_media.ops.sqlite_backup_cli import create_snapshot
    from relocant_media.ops.ssh_state_client import _state_digest
    from relocant_media.storage.sqlite_schema import initialize_sqlite
    database = tmp_path / 'source.sqlite3'
    initialize_sqlite(database)
    manifest = create_snapshot(database, tmp_path / 'saved')
    manifest['drive_file_id'] = manifest['sha256']
    busy = {**bootstrap_record(manifest, now=datetime.now(UTC)),
            'status': 'busy', 'attempt': 'attempt1', 'run_id': '123'}
    ready = {**busy, 'status': 'ready', 'generation': 'attempt1',
             'intake_state': {'attempts': 2, 'backup_pending': True}, 'last_exit_code': 1}
    checkpoint = {'version': 1, 'expected_busy_sha256': _state_digest(busy), 'state': ready}
    return checkpoint, busy, tmp_path / 'saved/editorial.sqlite3'


def test_prepare_verified_recovery_preserves_exact_state(recovery_fixture, tmp_path):
    from relocant_media.ops.recovery_checkpoint import prepare
    checkpoint, busy, snapshot = recovery_fixture
    output = tmp_path / 'prepared'
    audit = prepare(checkpoint, busy, snapshot, output, writers_fenced=True)
    request = json.loads((output / 'request.json').read_text())
    assert request == {'op': 'write_state',
                       'expected_sha': checkpoint['expected_busy_sha256'],
                       'state': checkpoint['state']}
    assert audit['applied'] is False
    assert (output / 'verified/editorial.sqlite3').read_bytes() == snapshot.read_bytes()


@pytest.mark.parametrize('mismatch', ['pointer', 'unfenced', 'snapshot'])
def test_prepare_refuses_inconsistent_recovery(recovery_fixture, tmp_path, mismatch):
    from relocant_media.ops.recovery_checkpoint import prepare
    checkpoint, busy, snapshot = recovery_fixture
    if mismatch == 'pointer':
        busy['run_id'] = 'new-writer'
    elif mismatch == 'snapshot':
        snapshot.write_bytes(b'corrupt')
    output = tmp_path / 'prepared'
    with pytest.raises(ValueError):
        prepare(checkpoint, busy, snapshot, output, writers_fenced=mismatch != 'unfenced')
    assert not (output / 'request.json').exists()


@pytest.mark.parametrize("stopped", [True, False])
def test_actions_recovery_applies_only_verified_stopped_attempt(
    recovery_fixture, tmp_path, monkeypatch, stopped,
):
    from relocant_media.ops import actions_recovery
    from relocant_media.ops.ssh_state_client import _state_digest

    checkpoint, busy, snapshot = recovery_fixture
    busy.update(run_attempt="1", started_at="2026-11-11T19:55:42+00:00")
    state = checkpoint["state"]
    state["run_attempt"] = "1"
    checkpoint["expected_busy_sha256"] = _state_digest(busy)
    envelope = json.dumps(checkpoint).encode()
    digest = hashlib.sha256(envelope).hexdigest()
    blobs = {digest: envelope, state["snapshot"]["sha256"]: snapshot.read_bytes()}

    class Storage:
        current = busy
        writes = 0

        def read(self):
            return self.current, _state_digest(self.current)

        def download(self, sha):
            return blobs[sha]

        def write(self, value, expected):
            assert expected == _state_digest(busy)
            self.writes += 1
            self.current = value
            return _state_digest(value)

    def verify(run_id, attempt):
        assert (run_id, attempt) == ("123", "1")
        if not stopped:
            raise ValueError("writer still running")

    monkeypatch.setattr(actions_recovery, "verify_stopped_run", verify)
    storage = Storage()
    if stopped:
        report = actions_recovery.recover(storage, "123", digest, tmp_path / "actions")
        assert report["recovered_run_id"] == "123"
        assert storage.current == state
        assert storage.writes == 1
    else:
        with pytest.raises(ValueError, match="writer still running"):
            actions_recovery.recover(storage, "123", digest, tmp_path / "actions")
        assert storage.current == busy
        assert storage.writes == 0


@pytest.mark.parametrize("visible_post", [103, 104])
def test_owner_rollback_checks_public_channel_and_preserves_receipts(
    recovery_fixture, tmp_path, monkeypatch, visible_post,
):
    from relocant_media.ops import actions_recovery
    from relocant_media.ops.sqlite_backup_cli import create_snapshot

    _, busy, _ = recovery_fixture
    source = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute(
            "INSERT INTO post (city,category,status,title,body,confidence,telegram_message_id) "
            "VALUES ('belgrade','events','published','Title','Body',1,103)",
        )
        db.commit()
    manifest = create_snapshot(source, tmp_path / "published")
    manifest["drive_file_id"] = manifest["sha256"]
    busy.update(snapshot=manifest, run_attempt="1", intake_state={"attempts": 2})
    client = Mock()
    client.read.side_effect = lambda: (client.current, "a" * 64)
    client.current = busy
    client.download.return_value = (tmp_path / "published/editorial.sqlite3").read_bytes()

    def write(value, expected):
        assert expected == "a" * 64
        client.current = value
        return expected

    client.write.side_effect = write
    stopped = Mock()
    monkeypatch.setattr(actions_recovery, "verify_stopped_run", stopped)
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, text=f'<div data-post="eduvbelgrad/{visible_post}"></div>',
    )))
    monkeypatch.setattr(actions_recovery.httpx, "Client", lambda **kwargs: http)
    args = (client, "123", manifest["created_at"], 103, tmp_path / "rollback")
    if visible_post == 103:
        audit = actions_recovery.rollback(*args)
        client.write.assert_called_once()
        assert client.current["status"] == "ready"
        assert client.current["generation"] == busy["attempt"]
        assert client.current["intake_state"] == busy["intake_state"]
        assert client.current["snapshot"] == manifest
        assert client.current["last_manual_recovery"] == audit
    else:
        with pytest.raises(ValueError, match="public channel differs"):
            actions_recovery.rollback(*args)
        client.write.assert_not_called()
    stopped.assert_called_once_with("123", "1")
