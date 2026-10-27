import ast
import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from relocant_media.ops import ssh_state_storage as storage


@pytest.fixture
def context(tmp_path, monkeypatch):
    root = tmp_path / 'storage'
    root.mkdir(mode=0o700)
    marker = tmp_path / 'cutover.json'
    content = b'opaque snapshot bytes, never interpreted as SQLite'
    digest = hashlib.sha256(content).hexdigest()
    marker.write_text(json.dumps({'version': 1, 'server_writers_fenced': True,
        'snapshot_sha256': digest, 'snapshot_bytes': len(content)}))
    marker.chmod(0o644)
    monkeypatch.setattr(storage, 'TRUSTED_MARKER_UID', os.geteuid())
    backend = storage.Storage(root, marker)
    assert backend.put_blob(digest, len(content), io.BytesIO(content)) == {'blob_id': digest}
    state = {'version': 1, 'owner': 'github-actions', 'status': 'ready', 'generation': 'seed',
        'snapshot': {'sha256': digest, 'drive_file_id': digest, 'bytes': len(content)},
        'cutover': {'server_fenced': True}, 'intake_state': {'attempts': 2}}
    return backend, state, content


def request(backend, payload, body=b''):
    outgoing = io.BytesIO()
    storage.serve(backend, io.BytesIO(json.dumps(payload).encode() + b'\n' + body), outgoing)
    header, separator, content = outgoing.getvalue().partition(b'\n')
    assert separator
    return json.loads(header), content


def initialize(context):
    backend, state, _ = context
    result, _ = request(backend, {'op': 'create_state', 'state': state})
    return result['sha']


def busy(state, attempt='attempt-1'):
    return {**copy.deepcopy(state), 'status': 'busy', 'attempt': attempt}


def test_bootstrap_private_atomic_state_and_exact_readback(context):
    backend, state, _ = context
    sha = initialize(context)
    result, body = request(backend, {'op': 'read_state'})
    assert result == {'state': state, 'sha': sha} and body == b''
    assert sha == hashlib.sha256(backend.pointer.read_bytes()).hexdigest()
    assert stat.S_IMODE(backend.pointer.stat().st_mode) == 0o600
    assert stat.S_IMODE((backend.root / 'state.lock').stat().st_mode) == 0o600
    for snapshot in (state, busy(state)):
        with pytest.raises(storage.StorageError) as error:
            backend.create_state(snapshot)
        assert error.value.code in {'state_exists', 'bootstrap_blocked'}
    assert backend.read_state()['sha'] == sha


def test_only_matching_busy_attempt_can_commit_and_retry_old_sha_conflicts(context):
    backend, state, _ = context
    original = initialize(context)
    claimed = busy(state)
    current = backend.write_state(original, claimed)['sha']
    assert backend.read_state()['state']['intake_state'] == {'attempts': 2}
    with pytest.raises(storage.StorageError, match='conflict'):
        backend.write_state(original, busy(state, 'loser'))
    with pytest.raises(storage.StorageError):
        backend.write_state(current, {**state, 'attempt': 'other', 'generation': 'other'})
    with pytest.raises(storage.StorageError):
        backend.write_state(current, busy(state, 'reset'))
    ready = {**claimed, 'status': 'ready', 'generation': claimed['attempt']}
    result = backend.write_state(current, ready)
    assert backend.read_state() == {'state': ready, 'sha': result['sha']}
    with pytest.raises(storage.StorageError):
        backend.write_state(result['sha'], state)


@pytest.mark.parametrize('field,value', [('server_writers_fenced', False),
    ('snapshot_sha256', '0' * 64), ('snapshot_bytes', 999)])
def test_bootstrap_requires_matching_admin_fence(context, field, value):
    backend, state, _ = context
    marker = json.loads(backend.marker.read_text())
    marker[field] = value
    backend.marker.write_text(json.dumps(marker))
    with pytest.raises(storage.StorageError, match='bootstrap_blocked'):
        backend.create_state(state)
    assert not backend.pointer.exists()


def test_writable_or_symlinked_marker_is_rejected(context):
    backend, state, _ = context
    backend.marker.chmod(0o666)
    with pytest.raises(storage.StorageError, match='bootstrap_blocked'):
        backend.create_state(state)
    actual = backend.marker.with_suffix('.actual')
    backend.marker.rename(actual)
    actual.chmod(0o644)
    backend.marker.symlink_to(actual)
    with pytest.raises(storage.StorageError, match='bootstrap_blocked'):
        backend.create_state(state)
    assert not backend.pointer.exists()


def test_blob_roundtrip_immutable_and_header_never_contains_private_bytes(context):
    backend, state, content = context
    header, body = request(backend, {'op': 'get_blob', 'blob_id': state['snapshot']['sha256']})
    assert body == content and header == {'sha256': hashlib.sha256(content).hexdigest(),
                                         'size': len(content)}
    original = (backend.blobs / header['sha256']).read_bytes()
    request(backend, {'op': 'put_blob', 'sha256': header['sha256'], 'size': len(content)}, content)
    with pytest.raises(storage.StorageError):
        request(backend, {'op': 'put_blob', 'sha256': header['sha256'], 'size': 5}, b'wrong')
    assert (backend.blobs / header['sha256']).read_bytes() == original
    assert stat.S_IMODE((backend.blobs / header['sha256']).stat().st_mode) == 0o600
    assert not list(backend.blobs.glob('.upload-*'))


@pytest.mark.parametrize('payload', [b'short', b'correct-extra'])
def test_truncated_extra_or_wrong_hash_upload_cannot_be_installed(context, payload):
    backend, _, _ = context
    expected = hashlib.sha256(b'correct').hexdigest()
    with pytest.raises(storage.StorageError):
        request(backend, {'op': 'put_blob', 'sha256': expected, 'size': 7}, payload)
    assert not (backend.blobs / expected).exists()


def test_blob_io_is_bounded_streaming(context):
    backend, _, _ = context
    content = b'x' * (storage.CHUNK * 3 + 17)
    class BoundedReader(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= storage.CHUNK
            return super().read(size)
    digest = hashlib.sha256(content).hexdigest()
    backend.put_blob(digest, len(content), BoundedReader(content))
    result, body = request(backend, {'op': 'get_blob', 'blob_id': digest})
    assert result['size'] == len(content) and body == content


@pytest.mark.parametrize('blob_id', ['../current.json', '/etc/passwd', 'x' * 64, 'ab/../cd'])
def test_paths_and_unknown_operations_are_not_a_remote_filesystem_api(context, blob_id):
    backend, _, _ = context
    with pytest.raises(storage.StorageError):
        request(backend, {'op': 'get_blob', 'blob_id': blob_id})
    with pytest.raises(storage.StorageError):
        request(backend, {'op': 'run', 'command': 'anything'})


def test_bounded_headers_state_and_blob_sizes(context, monkeypatch):
    backend, state, _ = context
    with pytest.raises(storage.StorageError):
        storage.serve(backend, io.BytesIO(b'x' * (storage.MAX_HEADER + 1)), io.BytesIO())
    monkeypatch.setattr(storage, 'MAX_STATE', 128)
    with pytest.raises(storage.StorageError):
        backend.create_state({**state, 'private': 'x' * 1024})
    with pytest.raises(storage.StorageError):
        backend.put_blob('0' * 64, storage.MAX_BLOB + 1, io.BytesIO())
    assert not backend.pointer.exists()


def test_uncertain_cas_reply_cannot_reclaim_or_reset_busy(context, monkeypatch):
    backend, state, _ = context
    original = initialize(context)
    original_sync = storage.sync_directory
    def fail_after_atomic_install(path):
        if path == backend.root:
            raise OSError('simulated missing acknowledgement')
        original_sync(path)
    monkeypatch.setattr(storage, 'sync_directory', fail_after_atomic_install)
    with pytest.raises(OSError):
        backend.write_state(original, busy(state))
    monkeypatch.setattr(storage, 'sync_directory', original_sync)
    observed = backend.read_state()
    assert observed['state']['status'] == 'busy'
    with pytest.raises(storage.StorageError, match='conflict'):
        backend.write_state(original, busy(state, 'retry'))
    with pytest.raises(storage.StorageError):
        backend.write_state(observed['sha'], state)


def test_cross_process_claims_have_exactly_one_winner(context):
    backend, state, _ = context
    sha = initialize(context)
    command = [sys.executable, str(Path(storage.__file__)), '--root', str(backend.root),
               '--cutover-marker', str(backend.marker)]
    children = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE) for _ in range(2)]
    for i, child in enumerate(children):
        assert child.stdin is not None
        child.stdin.write(json.dumps({'op': 'write_state', 'expected_sha': sha,
            'state': busy(state, f'claim-{i}')}).encode() + b'\n')
        child.stdin.close()
        child.stdin = None
    results = [child.communicate(timeout=10) for child in children]
    assert sorted(child.returncode for child in children) == [0, 1]
    responses = [json.loads(out) for out, _ in results]
    assert sum('sha' in value for value in responses) == 1
    assert sum(value.get('error') == 'conflict' for value in responses) == 1
    assert backend.read_state()['state']['status'] == 'busy'


def test_helper_is_standalone_stdlib_and_does_not_report_private_exception_text(context):
    backend, _, _ = context
    module = Path(storage.__file__)
    tree = ast.parse(module.read_text())
    modules = {node.module.split('.')[0] for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and node.module}
    modules |= {alias.name.split('.')[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    assert modules <= sys.stdlib_module_names | {'__future__'}
    result = subprocess.run([sys.executable, str(module), '--root', str(backend.root)],
        input=b'{"op":"read_state","token":"PRIVATE_SECRET"}\n', capture_output=True, check=False)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {'error': 'invalid_request'}
    assert b'PRIVATE_SECRET' not in result.stdout + result.stderr


def test_health_is_safe_before_fencing_and_never_exposes_pointer(context):
    backend, state, _ = context
    assert request(backend, {'op': 'health'}) == (
        {'storage_ready': True, 'bootstrapped': False}, b'')
    sha = initialize(context)
    backend.write_state(sha, busy(state))
    assert request(backend, {'op': 'health'}) == (
        {'storage_ready': True, 'bootstrapped': True}, b'')
    backend.pointer.write_text('invalid-pointer')
    with pytest.raises(ValueError):
        request(backend, {'op': 'health'})
