import ast
import copy
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from relocant_media.ai.package import canonical_json
from relocant_media.ops import ingress_cutover
from relocant_media.ops import ssh_state_storage as storage
from relocant_media.ops.actions_runtime import _commit_republication
from relocant_media.ops.serverless_snapshot import form_settings
from relocant_media.ops.ssh_state_client import SSHStateClient, SSHStorageError
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID


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
    assert type(result.pop('capacity_low')) is bool
    assert result == {'state': state, 'sha': sha, 'blob_readback': 'sha256-v1'}
    assert body == b''
    assert sha == hashlib.sha256(backend.pointer.read_bytes()).hexdigest()
    assert stat.S_IMODE(backend.pointer.stat().st_mode) == 0o600
    assert stat.S_IMODE((backend.root / 'state.lock').stat().st_mode) == 0o600
    for snapshot in (state, busy(state)):
        with pytest.raises(storage.StorageError) as error:
            backend.create_state(snapshot)
        assert error.value.code in {'state_exists', 'bootstrap_blocked'}
    assert backend.read_state()['sha'] == sha


@pytest.mark.parametrize(('total', 'free', 'warning'), [
    (20 * storage.CLAIM_HEADROOM, 5 * storage.CLAIM_HEADROOM, False),
    (20 * storage.CLAIM_HEADROOM, 4 * storage.CLAIM_HEADROOM, True),
    (5 * storage.CLAIM_HEADROOM, 2 * storage.CLAIM_HEADROOM, False),
    (5 * storage.CLAIM_HEADROOM, 2 * storage.CLAIM_HEADROOM - 1, True),
])
def test_capacity_warning_is_early_and_does_not_touch_pointer(context, monkeypatch, total, free,
                                                               warning):
    backend, state, _ = context
    sha = initialize(context)
    monkeypatch.setattr(storage.shutil, 'disk_usage',
                        lambda _: SimpleNamespace(total=total, free=free))
    result, body = request(backend, {'op': 'read_state'})
    assert result == {
        'state': state, 'sha': sha, 'capacity_low': warning, 'blob_readback': 'sha256-v1',
    }
    assert body == b''
    assert backend.read_state() == {'state': state, 'sha': sha}


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


def test_republication_receipts_commit_through_ssh_storage(context, tmp_path, monkeypatch):
    backend, state, _ = context
    digest = initialize(context)
    client = SSHStateClient(None)
    monkeypatch.setattr(client, 'exchange', lambda payload, body=b'': request(
        backend, payload, body,
    ))
    env = {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
    for phase in ('sending', 'sent'):
        history = {'97': {'state': phase}}
        state, digest = _commit_republication(
            client, client, state, digest, {'map_republications': history}, env, tmp_path,
        )
        assert backend.read_state() == {'state': state, 'sha': digest}
        assert state['status'] == 'ready' and state['generation'] == state['attempt']
        assert state['map_republications'] == history
        assert state['snapshot'] == context[1]['snapshot']
        assert state['intake_state'] == context[1]['intake_state']
        envelope = json.loads((tmp_path / f"republication-{state['attempt']}.json").read_text())
        assert envelope['state'] == state


@pytest.fixture
def ingress_context(context, monkeypatch):
    backend, state, _ = context
    state['intake_state'].update(signature='17,18', backup_pending=True, serverless_sequence=0)
    state['map_republications'] = {'97': {'state': 'sent'}}
    initialize(context)
    trace, writes, checkpoints = [], [], []
    fault = {}
    seed = {
        'version': 1, 'created_at': '2026-11-18T12:00:00+00:00', 'bot_id': 123456,
        'channel_id': APPROVED_CHANNEL_ID, 'form': json.loads(canonical_json(form_settings())),
        'processed_updates': [100], 'legacy_conversations': [],
        'cases': [{'id': 17, 'reference': 'RM-1234-5678', 'kind': 'technical',
                   'status': 'collecting', 'telegram_user_id': 42, 'telegram_message_id': 101,
                   'payload': {'reader_intake': True, 'public_reference': 'RM-1234-5678',
                               'chat_id': 42, 'text': 'Закрытый текст открытого черновика',
                               'reader_notices': {}}}],
    }
    client = SSHStateClient(None)

    def exchange(payload, body=b''):
        operation = payload['op']
        if operation == 'put_blob':
            value = json.loads(body)
            if 'expected_busy_sha256' in value:
                checkpoints.append(value)
                phase = value['state']['intake_state'].get('ingress', {}).get('phase', 'polling')
                trace.append(f'checkpoint:{phase}')
            elif value.get('operation') in {'rollback-empty', 'migrate-redis'}:
                trace.append(f"audit:{value['phase']}")
            else:
                trace.append('seed')
        if operation == 'write_state':
            target = payload['state']
            if target['status'] == 'busy':
                assert checkpoints
                assert checkpoints[-1]['expected_busy_sha256'] == hashlib.sha256(
                    storage.encode(target),
                ).hexdigest()
                old_phase = target['intake_state'].get('ingress', {}).get('phase', 'unprepared')
                label = f'busy:{old_phase}'
            else:
                phase = target['intake_state'].get('ingress', {}).get('phase', 'polling')
                if fault.get('ready_phase') == phase:
                    raise SSHStorageError('storage_error')
                label = phase
            result = request(backend, payload, body)
            writes.append(copy.deepcopy(target))
            trace.append(label)
            return result
        return request(backend, payload, body)

    monkeypatch.setattr(client, 'exchange', exchange)
    monkeypatch.setattr(ingress_cutover, '_export', lambda *args: copy.deepcopy(seed))
    webhook = {'url': '', 'max_connections': 1}
    bridge, secret = 'b' * 40, 's' * 40

    def telegram(request):
        operation = request.url.path.rsplit('/', 1)[-1]
        trace.append(operation)
        payload = json.loads(request.content)
        if operation == 'getMe':
            result = {'id': 123456, 'is_bot': True, 'username': ingress_cutover.BOT_USERNAME}
        elif operation == 'getChatMember':
            result = {'can_manage_direct_messages': True}
        elif operation == 'getWebhookInfo':
            result = copy.deepcopy(webhook)
        elif operation == 'transfer' and payload.get('operation') == 'storage-info':
            assert request.headers['Authorization'] == f'Bearer {bridge}'
            return httpx.Response(200, json=copy.deepcopy(fault['redis_storage_info']))
        elif operation == 'transfer' and payload.get('operation') == 'metadata':
            assert request.headers['Authorization'] == f'Bearer {bridge}'
            return httpx.Response(200, json={
                'status': 'ready',
                'state': {'etag_sha256': 'd' * 64, 'size': 7060,
                          'uploaded_at': '2020-01-01T00:00:00.000Z'},
                'config': {'etag_sha256': 'e' * 64, 'size': 259,
                           'uploaded_at': '2020-01-01T00:00:00.000Z'},
            })
        elif operation == 'deleteWebhook':
            recorded = backend.read_state()['state']
            assert recorded['status'] == 'ready'
            assert recorded['intake_state']['ingress']['phase'] == 'prepared'
            assert recorded['ingress_rollback']['phase'] == 'prepared'
            assert len(writes) == 6
            assert payload == {'drop_pending_updates': False}
            webhook['url'] = ''
            result = True
        elif operation in {'transfer', 'setWebhook'}:
            recorded = backend.read_state()['state']
            assert recorded['status'] == 'ready'
            assert recorded['intake_state']['ingress']['phase'] == 'prepared'
            assert len(writes) == (10 if recorded.get('ingress_migration') else 2)
            if operation == 'transfer':
                assert request.headers['Authorization'] == f'Bearer {bridge}'
                assert payload['operation'] == 'bootstrap' and payload['mode'] == 'active'
                assert json.loads(payload['seed_json']) == seed
                assert client.download(payload['seed_sha256']) == canonical_json(seed).encode()
                if 'redis_storage_info' in fault:
                    fault['redis_storage_info'] = {
                        'status': 'ready', 'backend': 'redis', 'namespace': 'production',
                        'initialized': True, 'seed_sha256': payload['seed_sha256'],
                        'mode': 'active',
                    }
                return httpx.Response(200, json={'status': 'ready', 'can_send': True,
                                                'seed_sha256': payload['seed_sha256']})
            assert payload == {'url': ingress_cutover.PRODUCTION_ORIGIN + '/api/telegram',
                               'secret_token': secret, 'max_connections': 1,
                               'drop_pending_updates': False}
            webhook['url'] = payload['url']
            result = True
        else:
            pytest.fail('Непредусмотренный вызов API в тесте переноса')
        return httpx.Response(200, json={'ok': True, 'result': result})

    with httpx.Client(transport=httpx.MockTransport(telegram), trust_env=False) as http:
        yield client, http, trace, writes, checkpoints, fault, seed


def execute_ingress(scenario, work):
    client, http, *_ = scenario
    return ingress_cutover.execute(
        client, ingress_cutover.PRODUCTION_ORIGIN, work, http,
        '123456:FAKE_TOKEN_FOR_TESTING_ONLY_123456', 'b' * 40, 's' * 40,
        activate=True, now=datetime(2026, 11, 18, 12, tzinfo=UTC),
    )


def test_ingress_cutover_uses_real_ssh_storage_transactions(context, ingress_context, tmp_path):
    backend, initial, snapshot = context
    client, _, trace, writes, checkpoints, _, seed = ingress_context
    result = execute_ingress(ingress_context, tmp_path / 'cutover')
    assert result['status'] == 'active' and result['open_drafts'] == 1
    assert trace == ['getMe', 'getChatMember', 'getWebhookInfo', 'seed', 'checkpoint:prepared',
                     'busy:unprepared', 'prepared', 'transfer', 'setWebhook', 'getWebhookInfo',
                     'checkpoint:active', 'busy:prepared', 'active']
    assert [state['status'] for state in writes] == ['busy', 'ready', 'busy', 'ready']
    assert len(checkpoints) == 2
    current, digest = client.read()
    assert backend.read_state() == {'state': current, 'sha': digest}
    assert current['generation'] == current['attempt']
    assert current['intake_state']['ingress']['phase'] == 'active'
    for state in writes:
        assert state['snapshot'] == initial['snapshot']
        assert state['cutover'] == initial['cutover']
        assert state['map_republications'] == initial['map_republications']
        for key, value in initial['intake_state'].items():
            assert state['intake_state'][key] == value
    assert client.download(initial['snapshot']['drive_file_id']) == snapshot
    seed_id = current['intake_state']['ingress']['seed_sha256']
    assert json.loads(client.download(seed_id))['cases'] == seed['cases']
    assert len(list((tmp_path / 'cutover').glob('republication-*.json'))) == 2


@pytest.mark.parametrize('phase', ['prepared', 'active'])
def test_ingress_failed_ssh_ready_commit_preserves_busy_fence(
    context, ingress_context, tmp_path, phase,
):
    backend, initial, _ = context
    client, _, trace, writes, checkpoints, fault, _ = ingress_context
    fault['ready_phase'] = phase
    with pytest.raises(SSHStorageError):
        execute_ingress(ingress_context, tmp_path / 'failed-cutover')
    observed = client.read()
    recorded = backend.read_state()['state']
    assert recorded['status'] == 'busy' and recorded['snapshot'] == initial['snapshot']
    assert len(writes) == (1 if phase == 'prepared' else 3)
    assert checkpoints[-1]['state']['intake_state']['ingress']['phase'] == phase
    if phase == 'prepared':
        assert 'ingress' not in recorded['intake_state']
        assert 'transfer' not in trace and 'setWebhook' not in trace
    else:
        assert recorded['intake_state']['ingress']['phase'] == 'prepared'
    fault.clear()
    trace.clear()
    with pytest.raises(ValueError, match='canonical state not ready'):
        execute_ingress(ingress_context, tmp_path / 'blocked-retry')
    assert client.read() == observed
    assert 'transfer' not in trace and 'setWebhook' not in trace


@pytest.mark.parametrize('failure', [False, True])
def test_empty_ingress_rollback_uses_real_ssh_storage_without_discarding_state(
    context, ingress_context, tmp_path, monkeypatch, failure,
):
    backend, initial, snapshot = context
    client, http, trace, writes, checkpoints, fault, seed = ingress_context
    monkeypatch.setenv('GITHUB_RUN_ID', '123456789')
    execute_ingress(ingress_context, tmp_path / 'activate-for-rollback')
    activation_checkpoint = hashlib.sha256(storage.encode(checkpoints[-1])).hexdigest()
    initial_blobs = {path.name for path in backend.blobs.iterdir()}
    trace.clear()

    def quiesce(seconds):
        assert seconds == 45
        recorded = backend.read_state()['state']
        assert recorded['status'] == 'ready'
        assert recorded['intake_state']['ingress']['phase'] == 'prepared'
        assert recorded['ingress_rollback']['phase'] == 'prepared'
        trace.append('quiesced')

    monkeypatch.setattr(ingress_cutover.time, 'sleep', quiesce)
    if failure:
        fault['ready_phase'] = 'polling'

    def rollback():
        return ingress_cutover.rollback_empty(
            client, ingress_cutover.PRODUCTION_ORIGIN, tmp_path / 'rollback', http,
            '123456:FAKE_TOKEN_FOR_TESTING_ONLY_123456', 'b' * 40, 'd' * 64,
            activation_checkpoint, '123456789', now=datetime(2026, 11, 18, 13, tzinfo=UTC),
        )

    if failure:
        with pytest.raises(SSHStorageError):
            rollback()
    else:
        assert rollback()['status'] == 'polling_restored'
    current, _ = client.read()
    assert current['status'] == ('busy' if failure else 'ready')
    assert current['ingress_rollback']['phase'] == ('prepared' if failure else 'complete')
    assert trace == [
        'getMe', 'getWebhookInfo', 'transfer', 'audit:prepared', 'checkpoint:prepared',
        'busy:active', 'prepared', 'deleteWebhook', 'getWebhookInfo', 'quiesced', 'transfer',
        'audit:complete', 'checkpoint:polling', 'busy:prepared', *([] if failure else ['polling']),
    ]
    assert [state['status'] for state in writes[4:]] == (
        ['busy', 'ready', 'busy'] if failure else ['busy', 'ready', 'busy', 'ready']
    )
    if failure:
        assert current['intake_state']['ingress']['phase'] == 'prepared'
    else:
        assert current['intake_state'] == initial['intake_state']
    assert current['snapshot'] == initial['snapshot']
    assert current['map_republications'] == initial['map_republications']
    assert initial_blobs <= {path.name for path in backend.blobs.iterdir()}
    assert client.download(initial['snapshot']['drive_file_id']) == snapshot
    assert len(checkpoints) == 4
    seed_id = current['ingress_rollback']['seed_sha256']
    assert json.loads(client.download(seed_id))['cases'] == seed['cases']


def test_redis_migration_after_rollback_preserves_real_storage_protocol_and_audits(
    context, ingress_context, tmp_path, monkeypatch,
):
    backend, initial, snapshot = context
    client, http, trace, writes, checkpoints, fault, seed = ingress_context
    monkeypatch.setenv('GITHUB_RUN_ID', '123456789')
    execute_ingress(ingress_context, tmp_path / 'activation-before-migration')
    checkpoint_id = hashlib.sha256(storage.encode(checkpoints[-1])).hexdigest()
    monkeypatch.setattr(ingress_cutover.time, 'sleep', lambda seconds: None)
    ingress_cutover.rollback_empty(
        client, ingress_cutover.PRODUCTION_ORIGIN, tmp_path / 'rollback-before-migration', http,
        '123456:FAKE_TOKEN_FOR_TESTING_ONLY_123456', 'b' * 40, 'd' * 64,
        checkpoint_id, '123456789', now=datetime(2026, 11, 18, 13, tzinfo=UTC),
    )
    rolled_back, _ = client.read()
    rollback_audit = copy.deepcopy(rolled_back['ingress_rollback'])
    previous_blobs = {path.name for path in backend.blobs.iterdir()}
    fault['redis_storage_info'] = {
        'status': 'ready', 'backend': 'redis', 'namespace': 'production', 'initialized': False,
    }
    seed['created_at'] = '2026-11-18T14:00:00+00:00'
    trace.clear()
    result = ingress_cutover.execute(
        client, ingress_cutover.PRODUCTION_ORIGIN, tmp_path / 'redis-migration', http,
        '123456:FAKE_TOKEN_FOR_TESTING_ONLY_123456', 'b' * 40, 's' * 40,
        activate=True, migrate_redis=True, now=datetime(2026, 11, 18, 14, tzinfo=UTC),
    )
    assert result['status'] == 'active'
    current, _ = client.read()
    assert current['status'] == 'ready' and current['generation'] == current['attempt']
    assert current['ingress_rollback'] == rollback_audit
    assert current['ingress_migration']['phase'] == 'active'
    assert current['ingress_migration']['backend'] == 'redis'
    assert current['ingress_migration']['seed_sha256'] == result['seed_sha256']
    assert current['ingress_migration']['rollback_audit_sha256'] == rollback_audit[
        'completion_audit_sha256']
    assert current['intake_state']['ingress']['phase'] == 'active'
    assert trace == [
        'getMe', 'getChatMember', 'getWebhookInfo', 'transfer', 'seed', 'audit:prepared',
        'checkpoint:prepared', 'busy:unprepared', 'prepared', 'transfer', 'transfer', 'transfer',
        'setWebhook', 'getWebhookInfo', 'checkpoint:active', 'busy:prepared', 'active',
    ]
    assert len(writes) == 12 and len(checkpoints) == 6
    assert [state['status'] for state in writes[8:]] == ['busy', 'ready', 'busy', 'ready']
    for state in writes[8:]:
        assert state['snapshot'] == initial['snapshot']
        assert state['cutover'] == initial['cutover']
        assert state['map_republications'] == initial['map_republications']
        for key, value in initial['intake_state'].items():
            assert state['intake_state'][key] == value
    assert previous_blobs < {path.name for path in backend.blobs.iterdir()}
    assert client.download(initial['snapshot']['drive_file_id']) == snapshot
    assert json.loads(client.download(result['seed_sha256'])) == seed


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


def test_verify_blob_reads_saved_file_without_returning_its_bytes(context):
    backend, state, content = context
    digest = state['snapshot']['sha256']
    check = {'op': 'verify_blob', 'blob_id': digest}
    header, body = request(backend, check)
    assert header == {'sha256': digest, 'size': len(content)} and body == b''
    with pytest.raises(storage.StorageError, match='invalid_request'):
        request(backend, check, b'extra')

    # Corrupt a stored byte without changing the file length or its name.
    (backend.blobs / digest).write_bytes(bytes([content[0] ^ 1]) + content[1:])
    with pytest.raises(storage.StorageError, match='storage_error'):
        request(backend, check)


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
        request(backend, {'op': 'verify_blob', 'blob_id': blob_id})
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



def test_identical_blob_reupload_is_idempotent_and_does_not_touch_pointer(context):
    backend, state, content = context
    initialize(context)
    before = backend.pointer.read_bytes()
    digest = state["snapshot"]["sha256"]
    for _ in range(3):
        assert backend.put_blob(digest, len(content), io.BytesIO(content)) == {"blob_id": digest}
    stream, size = backend.blob(digest)
    with stream:
        assert stream.read() == content
    assert size == len(content) and backend.pointer.read_bytes() == before
    assert [path.name for path in backend.blobs.iterdir()] == [digest]


def test_capacity_failure_happens_before_busy_and_does_not_retry_or_write(context, monkeypatch):
    backend, state, _ = context
    sha = initialize(context)
    original = backend.pointer.read_bytes()
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(storage.StorageError, match='storage_capacity'):
        backend.write_state(sha, busy(state))
    assert backend.pointer.read_bytes() == original
    assert backend.read_state()['state']['status'] == 'ready'


def test_capacity_guard_never_prevents_matching_busy_commit(context, monkeypatch):
    backend, state, _ = context
    sha = initialize(context)
    claimed = busy(state)
    sha = backend.write_state(sha, claimed)['sha']
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    ready = {**claimed, 'status': 'ready', 'generation': claimed['attempt']}
    backend.write_state(sha, ready)
    assert backend.read_state()['state'] == ready


def retention_fixture(backend):
    snapshots = []
    for number in range(13):
        content = b'SQLite format 3\0' + str(number).encode()
        sha = hashlib.sha256(content).hexdigest()
        path = backend.blobs / sha
        path.write_bytes(content)
        path.chmod(0o600)
        stamp = storage.time.time() - 20000 + number
        os.utime(path, (stamp, stamp))
        snapshots.append(path)
    today = datetime.now(UTC).date()
    proof = {'version': 1, 'verified_at': datetime.now(UTC).isoformat(), 'snapshots': [
        {'day': (today - timedelta(days=i)).isoformat(), 'sha256': path.name,
         'bytes': path.stat().st_size, 'archive_sha256': 'a' * 64,
         'drive_file_id': f'private-drive-copy-{i}'}
        for i, path in enumerate(snapshots[:3])]}
    path = backend.root / 'verified-retention.json'
    path.write_text(json.dumps(proof))
    path.chmod(0o644)
    return snapshots, proof


def test_gc_preserves_backups_checkpoint_recent_and_eight_newest_while_busy(context, monkeypatch):
    backend, state, _ = context
    original = initialize(context)
    backend.write_state(original, busy(state))
    before = backend.pointer.read_bytes()
    snapshots, _ = retention_fixture(backend)
    checkpoint = {'expected_busy_sha256': hashlib.sha256(before).hexdigest(),
                  'snapshot': {'sha256': snapshots[3].name}}
    payload = storage.encode(checkpoint)
    checkpoint_sha = hashlib.sha256(payload).hexdigest()
    backend.put_blob(checkpoint_sha, len(payload), io.BytesIO(payload))
    recent = snapshots[4]
    os.utime(recent, None)
    obsolete = snapshots[5]
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(
        free=0 if obsolete.exists() else storage.CLAIM_HEADROOM))
    with backend.locked():
        backend.ensure_space(storage.CLAIM_HEADROOM)
    assert not obsolete.exists()
    assert all(path.exists() for path in snapshots if path != obsolete)
    assert (backend.blobs / checkpoint_sha).exists()
    assert backend.pointer.read_bytes() == before


@pytest.mark.parametrize('invalid', [
    'expired', 'untrusted_mode', 'untrusted_owner', 'missing_blob', 'duplicate_day', 'old_day',
])
def test_gc_requires_three_recent_trusted_verified_copies(context, monkeypatch, invalid):
    backend, state, _ = context
    sha = initialize(context)
    snapshots, proof = retention_fixture(backend)
    path = backend.root / 'verified-retention.json'
    if invalid == 'expired':
        proof['verified_at'] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    elif invalid == 'duplicate_day':
        proof['snapshots'][1]['day'] = proof['snapshots'][0]['day']
    elif invalid == 'missing_blob':
        snapshots[0].unlink()
    elif invalid == 'old_day':
        proof['snapshots'][2]['day'] = (datetime.now(UTC) - timedelta(days=8)).date().isoformat()
    elif invalid == 'untrusted_owner':
        monkeypatch.setattr(storage, 'TRUSTED_MARKER_UID', os.geteuid() + 1)
    path.write_text(json.dumps(proof))
    if invalid == 'untrusted_mode':
        path.chmod(0o666)
    before = backend.pointer.read_bytes()
    existed = {p.name for p in backend.blobs.iterdir()}
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(storage.StorageError, match='storage_capacity'):
        backend.write_state(sha, busy(state))
    assert existed == {p.name for p in backend.blobs.iterdir()}
    assert backend.pointer.read_bytes() == before


def test_gc_does_not_follow_symlinks_or_delete_before_finishing_validation(context, monkeypatch):
    backend, state, _ = context
    sha = initialize(context)
    retention_fixture(backend)
    target = backend.root / 'outside-snapshot'
    payload = b'SQLite format 3\0outside'
    target.write_bytes(payload)
    target.chmod(0o600)
    link = backend.blobs / hashlib.sha256(payload).hexdigest()
    link.symlink_to(target)
    before = backend.pointer.read_bytes()
    existed = {p.name for p in backend.blobs.iterdir()}
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with pytest.raises(storage.StorageError, match='storage_capacity'):
        backend.write_state(sha, busy(state))
    assert link.is_symlink() and target.read_bytes() == payload
    assert existed == {p.name for p in backend.blobs.iterdir()}
    assert backend.pointer.read_bytes() == before


def test_gc_refuses_invalid_canonical_snapshot_before_deletion(context, monkeypatch):
    backend, state, _ = context
    initialize(context)
    retention_fixture(backend)
    state['snapshot']['sha256'] = state['snapshot']['drive_file_id'] = 'f' * 64
    backend.pointer.write_bytes(storage.encode(state))
    before = backend.pointer.read_bytes()
    existed = {p.name for p in backend.blobs.iterdir()}
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=0))
    with backend.locked(), pytest.raises(storage.StorageError, match='blob_missing'):
        backend.ensure_space(storage.CLAIM_HEADROOM)
    assert existed == {p.name for p in backend.blobs.iterdir()}
    assert backend.pointer.read_bytes() == before
