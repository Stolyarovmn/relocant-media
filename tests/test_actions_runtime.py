import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from relocant_media.ops import actions_runtime as runtime
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.models import Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

ROOT = Path(__file__).resolve().parents[1]


class MemoryStore:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.sha = 'initial'
        self.fail_commit = False

    def read(self):
        return copy.deepcopy(self.state), self.sha

    def write(self, state, sha):
        if sha != self.sha or (self.fail_commit and state['status'] == 'ready'):
            raise ValueError('conflict')
        self.state = copy.deepcopy(state)
        self.sha += 'x'
        return self.sha


class MemoryDrive:
    def __init__(self, content):
        self.files = {'original_id': content}
        self.fail_upload = False

    def download(self, file_id):
        return self.files[file_id]

    def upload(self, path, folder):
        if self.fail_upload:
            raise ValueError('Drive unavailable')
        self.files['saved_id_1'] = path.read_bytes()
        return 'saved_id_1'


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'source.sqlite3'
    initialize_sqlite(path)
    manifest = create_snapshot(path, tmp_path / 'original')
    manifest['drive_file_id'] = 'original_id'
    return runtime.bootstrap_record(manifest, now=datetime.now(UTC))


@pytest.fixture
def context(state, tmp_path):
    return MemoryStore(state), MemoryDrive((tmp_path / 'original/editorial.sqlite3').read_bytes())


def execute(context, tmp_path, monkeypatch, **kwargs):
    monkeypatch.setattr(runtime, 'export_public', lambda *args: None)
    env = {'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
           'GITHUB_REF': 'refs/heads/main', 'GH_STATE_TOKEN': 'SECRET',
           'DRIVE_CREDENTIALS_JSON': 'SECRET'}
    return runtime.execute(*context, work=tmp_path / 'work', output=tmp_path / 'public.json',
                           folder='folder_id_1', mode=kwargs.pop('mode', 'editorial'),
                           environment=kwargs.pop('environment', env), **kwargs)


def test_success_claims_before_side_effect_and_restores_receipts(context, tmp_path, monkeypatch):
    store, drive = context
    store.state['intake_state'] = {'signature': '1,2', 'attempts': 2}
    def child(command, env):
        assert store.state['status'] == 'busy'
        assert 'GH_STATE_TOKEN' not in env and 'DRIVE_CREDENTIALS_JSON' not in env
        database = Path(env['DATABASE_URL'].removeprefix('sqlite:///'))
        saved_budget = json.loads((database.parent / 'intake-batch-state.json').read_text())
        assert saved_budget['attempts'] == 2
        engine = create_engine(env['DATABASE_URL'])
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO source (id,city,name,url,source_type,trust,"
                "ownership,enabled,auto_publish,high_risk) VALUES "
                "('s','belgrade','Source','https://example.org','news',1,'official',1,0,0)"))
            for idx, status in enumerate(('sending', 'uncertain', 'sent'), 1):
                connection.execute(text("INSERT INTO raw_item "
                    "(id,source_id,external_id,url,title,body) "
                    "VALUES (:id,'s',:external,'https://example.org/a','Title','Body')"),
                    {'id': idx, 'external': str(idx)})
                connection.execute(text("INSERT INTO editorial_run "
                    "(raw_item_id,outcome,evidence,delivery_status) "
                    "VALUES (:id,'{}','{}',:status)"), {'id': idx, 'status': status})
            connection.execute(text("INSERT INTO telegram_update_receipt "
                "(update_id,kind,status) VALUES (9876,'message','processed')"))
        engine.dispose()
        return 0
    monkeypatch.setattr(runtime, 'run_child', child)
    assert execute(context, tmp_path, monkeypatch) == 0
    assert store.state['status'] == 'ready'
    assert store.state['intake_state']['attempts'] == 2
    digest = hashlib.sha256(drive.files[store.state['snapshot']['drive_file_id']]).hexdigest()
    assert store.state['snapshot']['sha256'] == digest
    recovery = tmp_path / 'verify-checkpoint'
    recovery.mkdir()
    database = runtime.restored(drive, store.state['snapshot'], recovery)
    engine = create_engine(f'sqlite:///{database}')
    with engine.connect() as connection:
        assert connection.execute(text('SELECT delivery_status FROM editorial_run '
            'ORDER BY raw_item_id')).scalars().all() == ['sending', 'uncertain', 'sent']
        offset = connection.execute(text('SELECT update_id FROM telegram_update_receipt')).scalar()
        assert offset == 9876
    engine.dispose()


@pytest.mark.parametrize('failure', ['crash', 'upload', 'commit'])
def test_uncertain_attempt_stays_busy_and_blocks_next(context, tmp_path, monkeypatch, failure):
    store, drive = context
    calls = []
    def child(*args):
        calls.append('telegram_accepted')
        if failure == 'crash':
            raise TimeoutError('after Telegram accepted, before receipt')
        engine = create_engine(args[1]['DATABASE_URL'])
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE simulated_effect (receipt TEXT)')
        engine.dispose()
        return 0
    monkeypatch.setattr(runtime, 'run_child', child)
    drive.fail_upload = failure == 'upload'
    store.fail_commit = failure == 'commit'
    with pytest.raises((TimeoutError, ValueError)):
        execute(context, tmp_path, monkeypatch)
    assert store.state['status'] == 'busy'
    with pytest.raises(ValueError, match='not ready'):
        execute(context, tmp_path / 'next-attempt', monkeypatch)
    assert calls == ['telegram_accepted']


def test_conflicting_claim_never_runs_worker(context, tmp_path, monkeypatch):
    store, _ = context
    def loser(*args):
        raise ValueError('concurrent CAS won elsewhere')
    monkeypatch.setattr(store, 'write', loser)
    monkeypatch.setattr(runtime, 'run_child', lambda *a: pytest.fail('must not call Telegram'))
    with pytest.raises(ValueError, match='concurrent'):
        execute(context, tmp_path, monkeypatch)


@pytest.mark.parametrize('which', ['corrupt', 'cutover', 'branch'])
def test_bad_input_blocks_before_claim_or_worker(context, tmp_path, monkeypatch, which):
    store, drive = context
    if which == 'corrupt':
        drive.files['original_id'] = b'corrupt'
    if which == 'cutover':
        store.state['cutover']['server_fenced'] = False
    env = {'GITHUB_REF': 'refs/heads/feature'}
    monkeypatch.setattr(runtime, 'run_child', lambda *a: pytest.fail('side effects'))
    with pytest.raises(ValueError):
        options = {'environment': env} if which == 'branch' else {}
        execute(context, tmp_path, monkeypatch, **options)
    assert store.state['status'] == 'ready'


def test_preview_keeps_observation_timestamp_and_does_not_claim(context, tmp_path, monkeypatch):
    store, _ = context
    expected = store.state['snapshot']['created_at']
    calls = []
    monkeypatch.setattr(runtime, 'run_child', lambda *a: pytest.fail('preview is read-only'))
    # execute helper installs export fake; invoke runtime directly here.
    monkeypatch.setattr(runtime, 'export_public', lambda *a: calls.append(a))
    assert runtime.execute(*context, work=tmp_path / 'preview', output=tmp_path / 'public.json',
                           folder='', mode='preview', environment={}) == 0
    assert calls[0][2] == expected
    assert store.sha == 'initial'


def test_stale_bootstrap_is_rejected(state):
    with pytest.raises(ValueError, match='fresh final snapshot'):
        runtime.bootstrap_record(state['snapshot'], now=datetime.now(UTC) + timedelta(days=1))


def test_discovery_claims_then_enters_existing_queue_and_checkpoints_receipt(
    context, tmp_path, monkeypatch,
):
    store, drive = context
    batch = json.dumps({'version': 1, 'city': 'belgrade',
                        'urls': ['https://www.beograd.rs/cir/beoinfo-vesti/a116374/work.html']})
    env = {'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
           'GITHUB_REF': 'refs/heads/main', 'CLOUD_DISCOVERY_BATCH': batch}
    calls = []
    def child(command, environment):
        assert store.state['status'] == 'busy'
        assert command[-1] == '--submissions-only'
        assert environment['EDITORIAL_INTAKE_MODE'] == 'batch'
        assert environment['EDITORIAL_PRIVATE_FEEDBACK_ENABLED'] == 'false'
        assert 'CLOUD_DISCOVERY_BATCH' not in environment
        engine = create_engine(environment['DATABASE_URL'])
        with Session(engine) as session:
            row = session.get(Submission, 1)
            assert row.payload['publication_evidence'] is False and row.status == 'new'
        engine.dispose()
        calls.append(command)
        return 1  # A failed worker still checkpoints accepted candidates for the next cycle.
    monkeypatch.setattr(runtime, 'run_child', child)
    assert execute(context, tmp_path, monkeypatch, mode='discovery', environment=env) == 1
    assert store.state['status'] == 'ready'
    receipt = store.state['last_discovery']
    assert receipt['created'] == 1 and receipt['runtime_exit_code'] == 1
    assert len(receipt['batch_sha256']) == 64
    assert receipt['candidates'][0]['intake_status'] == 'new'
    assert receipt['candidates'][0]['deliveries'] == []
    # A replay restores the committed DB, observes the same candidate and calls no worker.
    (tmp_path / 'again').mkdir()
    assert execute(context, tmp_path / 'again', monkeypatch,
                   mode='discovery', environment=env) == 0
    assert len(calls) == 1
    assert store.state['last_discovery']['created'] == 0
    assert store.state['last_discovery']['candidates'][0]['submission_id'] == 1
    assert len(drive.files) == 2


@pytest.mark.parametrize('mode,batch', [
    ('discovery', '{"version":1,"city":"belgrade","urls":[],"ready":true}'),
    ('discovery', ''), ('preview', '{"version":1}'), ('editorial', '{"version":1}'),
    ('invalid', ''),
])
def test_invalid_discovery_never_claims_or_runs(context, tmp_path, monkeypatch, mode, batch):
    store, _ = context
    monkeypatch.setattr(runtime, 'run_child', lambda *a: pytest.fail('side effects'))
    with pytest.raises(ValueError):
        execute(context, tmp_path, monkeypatch, mode=mode,
                environment={'CLOUD_DISCOVERY_BATCH': batch})
    assert store.sha == 'initial'


@pytest.mark.parametrize('failure', ['claim', 'upload', 'commit', 'cutover'])
def test_discovery_requires_canonical_claim_and_durable_checkpoint(
    context, tmp_path, monkeypatch, failure,
):
    store, drive = context
    env = {'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
           'GITHUB_REF': 'refs/heads/main', 'CLOUD_DISCOVERY_BATCH': json.dumps({
               'version': 1, 'city': 'belgrade', 'urls': ['https://www.beograd.rs/example']})}
    calls = []
    monkeypatch.setattr(runtime, 'run_child', lambda *a: calls.append(a) or 0)
    if failure == 'claim':
        monkeypatch.setattr(store, 'write', lambda *a: (_ for _ in ()).throw(ValueError('CAS')))
    if failure == 'cutover':
        env['ACTIONS_CUTOVER_CONFIRMED'] = 'false'
    store.fail_commit = failure == 'commit'
    drive.fail_upload = failure == 'upload'
    with pytest.raises(ValueError):
        execute(context, tmp_path, monkeypatch, mode='discovery', environment=env)
    assert 'last_discovery' not in store.state
    if failure in {'claim', 'cutover'}:
        assert not calls and store.state['status'] == 'ready'
    else:
        assert len(calls) == 1 and store.state['status'] == 'busy'


def test_github_control_updates_have_sha_precondition():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(409, json={'message': 'conflict'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = runtime.StateStore(client, 'secret')
        with pytest.raises(httpx.HTTPStatusError):
            store.write({'status': 'busy'}, 'known-sha')
    assert json.loads(requests[0].content)['sha'] == 'known-sha'
    assert json.loads(requests[0].content)['branch'] == 'editorial-state'


def test_workflow_never_uploads_private_database_or_runs_on_pr():
    import yaml
    document = yaml.safe_load((ROOT / '.github/workflows/editorial-actions.yml').read_text())
    triggers = document.get('on', document.get(True))
    assert 'pull_request' not in triggers and 'push' not in triggers
    assert document['concurrency'] == {
        'group': 'canonical-editorial-writer', 'cancel-in-progress': False}
    steps = document['jobs']['cycle']['steps']
    uploads = [s for s in steps if s.get('uses', '').startswith('actions/upload-')]
    assert len(uploads) == 1 and uploads[0]['with']['path'] == '_site'
    assert 'ACTIONS_EDITORIAL_ENABLED' in document['jobs']['cycle']['if']


def test_drive_upload_requires_full_readback_hash(tmp_path, monkeypatch):
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == 'POST':
            return httpx.Response(200, json={'id': 'saved_id_1'})
        return httpx.Response(200, content=b'wrong bytes')
    path = tmp_path / 'snapshot'
    path.write_bytes(b'original bytes')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        drive = object.__new__(runtime.Drive)
        drive.client = client
        monkeypatch.setattr(drive, 'headers', lambda: {'Authorization': 'Bearer private'})
        with pytest.raises(ValueError, match='readback differs'):
            drive.upload(path, 'folder_id_1')
    assert len(calls) == 2
    assert calls[1].url.params['alt'] == 'media'


def test_intake_preserves_last_editorial_models(context, tmp_path, monkeypatch):
    from relocant_media.ops.newsroom_export import snapshot
    store, _ = context
    old = {'status': 'PRIMARY_OK', 'observed_at': '2026-10-25T00:00:00+00:00',
           'ai_calls': [{'provider': 'gemini', 'model': 'verified-model', 'task': 'facts'}]}
    store.state['last_cycle'] = old
    def exported(database, output, observed_at, run_dir):
        assert run_dir is None
        output.write_text(json.dumps(snapshot(ROOT)))
    monkeypatch.setattr(runtime, 'export_public', exported)
    monkeypatch.setattr(runtime, 'run_child', lambda *args: 0)
    output = tmp_path / 'public.json'
    runtime.execute(*context, work=tmp_path / 'work', output=output,
                    folder='folder_id_1', mode='intake', environment={
                        'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
                        'GITHUB_REF': 'refs/heads/main'})
    city = json.loads(output.read_text())['cities'][0]
    assert city['last_cycle']['observed_at'] == old['observed_at']
    assert city['last_cycle']['ai_calls'][0]['model'] == 'verified-model'


@pytest.mark.parametrize('interruption', [ValueError, KeyboardInterrupt])
def test_projection_never_enters_site_before_ready_cas(
    context, tmp_path, monkeypatch, interruption,
):
    from relocant_media.ops.newsroom_export import snapshot

    store, _ = context
    output = tmp_path / '_site/data.json'

    def exported(database, destination, observed_at, run_dir):
        assert destination != output
        destination.write_text(json.dumps(snapshot(ROOT)))

    original_write = store.write

    def interrupted(state, sha):
        assert not output.exists()
        if state['status'] == 'ready':
            raise interruption('checkpoint interrupted')
        return original_write(state, sha)

    monkeypatch.setattr(runtime, 'export_public', exported)
    monkeypatch.setattr(runtime, 'run_child', lambda *args: 0)
    monkeypatch.setattr(store, 'write', interrupted)
    with pytest.raises(interruption):
        runtime.execute(*context, work=tmp_path / 'work', output=output,
                        folder='folder_id_1', mode='editorial', environment={
                            'ACTIONS_CUTOVER_CONFIRMED': 'true',
                            'GITHUB_REPOSITORY': runtime.REPO, 'GITHUB_REF': 'refs/heads/main'})
    assert store.state['status'] == 'busy'
    assert not output.exists()
    assert (tmp_path / 'work/public-projection.json').is_file()
