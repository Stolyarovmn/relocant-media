import base64
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
           'GH_STATE_REPOSITORY': 'Owner/private-control',
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
        assert 'GH_STATE_REPOSITORY' not in env
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


@pytest.mark.parametrize(("minutes", "due"), [(None, True), (10, False), (54, False), (56, True)])
def test_editorial_is_due_once_the_hourly_interval_has_passed(minutes, due):
    now = datetime(2026, 10, 28, 12, tzinfo=UTC)
    state = {} if minutes is None else {
        'last_editorial_at': (now - timedelta(minutes=minutes)).isoformat()}
    assert runtime.editorial_due(state, now) is due


def test_auto_mode_runs_editorial_when_due_then_intake_until_the_next_hour(
        context, tmp_path, monkeypatch):
    commands = []
    def child(command, env):
        commands.append(command[2].rsplit('.', 1)[-1])
        return 0
    monkeypatch.setattr(runtime, 'run_child', child)
    assert execute(context, tmp_path / 'one', monkeypatch, mode='auto') == 0
    recorded = context[0].state['last_editorial_at']
    assert execute(context, tmp_path / 'two', monkeypatch, mode='auto') == 0
    assert commands == ['server_runtime', 'intake_batch']
    assert context[0].state['last_editorial_at'] == recorded


def test_editorial_child_receives_a_new_output_directory(context, tmp_path, monkeypatch):
    seen = []
    def child(command, env):
        output = Path(command[command.index('--output') + 1])
        seen.append(output.exists())
        output.mkdir()
        return 0
    monkeypatch.setattr(runtime, 'run_child', child)
    assert execute(context, tmp_path, monkeypatch) == 0
    assert seen == [False]


def test_editorial_failure_before_output_exists_still_checkpoints(
        context, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'run_child', lambda command, env: 1)
    assert execute(context, tmp_path, monkeypatch) == 1
    assert context[0].state['status'] == 'ready'


def test_failed_child_reports_only_whitelisted_failure_coordinates(
        context, tmp_path, monkeypatch, capsys):
    def child(command, env):
        output = Path(command[command.index('--output') + 1])
        output.mkdir()
        report = output / 'server-report.json'
        report.write_text(json.dumps({'status': 'SERVER_FAILED', 'error_type': 'ValueError',
            'error_site': 'server_cycle.py:816', 'secret': 'TOKEN-123',
            'backup_error_type': 'bad value with spaces'}), encoding='utf-8')
        return 1
    monkeypatch.setattr(runtime, 'run_child', child)
    assert execute(context, tmp_path, monkeypatch) == 1
    err = capsys.readouterr().err
    failure = json.loads(err.strip().splitlines()[-1])['runtime_failure']
    assert failure == {'status': 'SERVER_FAILED', 'error_type': 'ValueError',
                       'error_site': 'server_cycle.py:816'}
    assert 'TOKEN-123' not in err


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
        if request.method == 'GET':
            return httpx.Response(200, json={'full_name': runtime.REPO, 'private': True})
        return httpx.Response(409, json={'message': 'conflict'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = runtime.StateStore(client, 'secret')
        with pytest.raises(httpx.HTTPStatusError):
            store.write({'status': 'busy'}, 'known-sha')
    assert json.loads(requests[1].content)['sha'] == 'known-sha'
    assert json.loads(requests[1].content)['branch'] == 'editorial-state'


@pytest.mark.parametrize('metadata', [
    {'full_name': runtime.REPO, 'private': False},
    {'full_name': runtime.REPO, 'private': 'true'},
    {'full_name': runtime.REPO}, {'full_name': 'other/repository', 'private': True},
])
@pytest.mark.parametrize('operation', ['read', 'write'])
def test_public_or_unknown_repository_never_exposes_state(metadata, operation):
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.path == f'/repos/{runtime.REPO}'
        return httpx.Response(200, json=metadata)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = runtime.StateStore(client, 'secret')
        with pytest.raises(ValueError, match='private repository required'):
            store.read() if operation == 'read' else store.write({'private': 'data'}, 'sha')
    assert len(requests) == 1


def test_privacy_is_rechecked_before_each_state_write():
    checks = []
    writes = []
    def handle(request):
        if request.method == 'GET':
            checks.append(request)
            return httpx.Response(200, json={
                'full_name': runtime.REPO, 'private': len(checks) == 1})
        writes.append(request)
        return httpx.Response(200, json={'content': {'sha': 'claimed'}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = runtime.StateStore(client, 'secret')
        assert store.write({'status': 'busy'}, 'initial') == 'claimed'
        with pytest.raises(ValueError, match='private repository required'):
            store.write({'status': 'ready'}, 'claimed')
    assert len(checks) == 2 and len(writes) == 1


def test_bootstrap_public_repository_is_blocked_before_drive_or_branch_creation(
    state, tmp_path, monkeypatch,
):
    import sys

    from relocant_media.ops import actions_bootstrap

    manifest = tmp_path / 'final.json'
    manifest.write_text(json.dumps(state['snapshot']))
    intake = tmp_path / 'intake.json'
    intake.write_text('{}')
    monkeypatch.setenv('GH_STATE_TOKEN', 'secret')
    monkeypatch.setattr(sys, 'argv', ['bootstrap', '--final-manifest', str(manifest),
        '--intake-state', str(intake), '--work', str(tmp_path / 'work'),
        '--confirm-all-former-writers-fenced'])
    requests = []
    def handle(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path == f'/repos/{runtime.REPO}'
        return httpx.Response(200, json={'full_name': runtime.REPO, 'private': False})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(actions_bootstrap.httpx, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(actions_bootstrap, 'Drive', lambda *args: pytest.fail('Drive access'))
    with pytest.raises(SystemExit) as error:
        actions_bootstrap.main()
    assert error.value.code == 1 and len(requests) == 1


def test_unknown_repository_privacy_blocks_state_network_operations():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(403, json={'message': 'metadata unavailable'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            runtime.StateStore(client, 'secret').write({'sensitive': 'state'}, 'sha')
    assert len(requests) == 1 and requests[0].method == 'GET'


@pytest.mark.parametrize('repository', [
    'https://github.com/Owner/state', 'Owner/state/extra', '../state', 'Owner/..',
    'Owner/state?token=secret', 'Owner/state#fragment', 'Owner\\state', '/Owner/state',
    ' Owner/state', 'Owner/state ', 'Owner/', '',
])
def test_state_repository_rejects_paths_urls_and_invalid_names(repository):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail('invalid repository must not reach network'),
    )) as client:
        with pytest.raises(ValueError, match='owner/name'):
            runtime.StateStore(client, 'secret', repository=repository)


def test_public_code_repository_uses_one_private_control_checkpoint(state, tmp_path, monkeypatch):
    control = 'Owner/private-control'
    checkpoint = copy.deepcopy(state)
    sha = 'initial'
    requests = []
    def handle(request):
        nonlocal checkpoint, sha
        requests.append(request)
        assert request.url.host == 'api.github.com'
        assert request.url.path.startswith(f'/repos/{control}')
        assert request.headers['Authorization'] == 'Bearer scoped-state-secret'
        if request.url.path == f'/repos/{control}':
            return httpx.Response(200, json={'full_name': control, 'private': True})
        assert request.url.path == f'/repos/{control}/contents/state/current.json'
        if request.method == 'GET':
            return httpx.Response(200, json={
                'content': base64.b64encode(json.dumps(checkpoint).encode()).decode(),
                'sha': sha,
            })
        body = json.loads(request.content)
        assert body['sha'] == sha and body['branch'] == 'editorial-state'
        checkpoint = json.loads(base64.b64decode(body['content']))
        sha += 'x'
        return httpx.Response(200, json={'content': {'sha': sha}})
    def child(command, env):
        assert checkpoint['status'] == 'busy'
        assert env['GITHUB_REPOSITORY'] == runtime.REPO
        assert 'GH_STATE_TOKEN' not in env and 'GH_STATE_REPOSITORY' not in env
        return 0
    monkeypatch.setattr(runtime, 'run_child', child)
    monkeypatch.setattr(runtime, 'export_public', lambda *args: None)
    drive = MemoryDrive((tmp_path / 'original/editorial.sqlite3').read_bytes())
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        store = runtime.StateStore(client, 'scoped-state-secret', repository=control)
        assert runtime.execute(store, drive, work=tmp_path / 'private-work',
            output=tmp_path / 'public.json', folder='folder_id_1', mode='editorial', environment={
                'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
                'GITHUB_REF': 'refs/heads/main', 'GH_STATE_REPOSITORY': control,
                'GH_STATE_TOKEN': 'scoped-state-secret'}) == 0
    assert checkpoint['status'] == 'ready' and sha == 'initialxx'
    assert len([r for r in requests if r.method == 'PUT']) == 2


@pytest.mark.parametrize('metadata', [
    {'full_name': 'Owner/private-control', 'private': False},
    {'full_name': 'Owner/other', 'private': True},
    {'full_name': runtime.REPO, 'private': True},
])
def test_selected_control_repository_requires_its_own_verified_privacy(metadata):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=metadata)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ValueError, match='private repository required'):
            runtime.StateStore(client, 'secret', repository='Owner/private-control').read()
    assert len(requests) == 1


def test_bootstrap_targets_private_repository_and_its_default_branch(state, tmp_path, monkeypatch):
    import base64
    import sys

    from relocant_media.ops import actions_bootstrap

    control = 'Owner/private-control'
    manifest = tmp_path / 'final.json'
    manifest.write_text(json.dumps(state['snapshot']))
    intake = tmp_path / 'intake.json'
    intake.write_text('{"attempts":2}')
    monkeypatch.setenv('GH_STATE_REPOSITORY', control)
    monkeypatch.setenv('GH_STATE_TOKEN', 'scoped-state-secret')
    monkeypatch.setattr(sys, 'argv', ['bootstrap', '--final-manifest', str(manifest),
        '--intake-state', str(intake), '--work', str(tmp_path / 'work'),
        '--confirm-all-former-writers-fenced'])
    requests, saved = [], []
    def handle(request):
        requests.append(request)
        assert request.url.path.startswith(f'/repos/{control}')
        if request.url.path == f'/repos/{control}':
            return httpx.Response(200, json={'full_name': control, 'private': True,
                                           'default_branch': 'control/trunk'})
        if request.url.path.endswith('/contents/state/current.json'):
            if request.method == 'GET':
                return httpx.Response(200, json={'content': base64.b64encode(
                    json.dumps(saved[0]).encode()).decode(), 'sha': 'created'}) if saved else (
                        httpx.Response(404))
            saved.append(json.loads(base64.b64decode(json.loads(request.content)['content'])))
            return httpx.Response(201, json={})
        if request.url.path.endswith('/git/ref/heads/editorial-state'):
            return httpx.Response(404)
        if request.method == 'GET':
            assert request.url.raw_path.endswith(b'/git/ref/heads/control%2Ftrunk')
            return httpx.Response(200, json={'object': {'sha': 'private-default-head'}})
        assert request.url.path.endswith('/git/refs')
        assert json.loads(request.content) == {
            'ref': 'refs/heads/editorial-state', 'sha': 'private-default-head'}
        return httpx.Response(201, json={})
    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(actions_bootstrap.httpx, 'Client', lambda **kwargs: client)
    monkeypatch.setattr(actions_bootstrap, 'Drive', lambda *args: object())
    restores = []
    monkeypatch.setattr(actions_bootstrap, 'restored', lambda *args: restores.append(args))
    actions_bootstrap.main()
    assert saved[0]['status'] == 'ready' and saved[0]['intake_state']['attempts'] == 2
    assert len(restores) == 1
    assert len([r for r in requests if r.method == 'POST']) == 1


@pytest.mark.parametrize('failure', ['commit', 'termination'])
def test_projection_is_not_publishable_before_ready_checkpoint(
    context, tmp_path, monkeypatch, failure,
):
    from relocant_media.ops.newsroom_export import snapshot
    store, _ = context
    output = tmp_path / '_site/data.json'
    def exported(database, provisional, observed_at, run_dir):
        assert provisional.parent == tmp_path / 'work'
        assert not output.exists()
        provisional.write_text(json.dumps(snapshot(ROOT)))
    original_write = store.write
    def checked_write(state, sha):
        assert not output.exists()
        if state['status'] == 'ready' and failure == 'termination':
            raise KeyboardInterrupt('simulated termination before checkpoint')
        return original_write(state, sha)
    monkeypatch.setattr(store, 'write', checked_write)
    store.fail_commit = failure == 'commit'
    monkeypatch.setattr(runtime, 'export_public', exported)
    monkeypatch.setattr(runtime, 'run_child', lambda *args: 0)
    with pytest.raises((ValueError, KeyboardInterrupt)):
        runtime.execute(*context, work=tmp_path / 'work', output=output,
                        folder='folder_id_1', mode='editorial', environment={
                            'ACTIONS_CUTOVER_CONFIRMED': 'true',
                            'GITHUB_REPOSITORY': runtime.REPO, 'GITHUB_REF': 'refs/heads/main'})
    assert not output.exists()
    assert store.state['status'] == 'busy'
    assert (tmp_path / 'work/public-projection.json').is_file()


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
    worker = next(step for step in steps if step.get('id') == 'runtime')
    assert worker['env']['GH_STATE_REPOSITORY'] == '${{ vars.EDITORIAL_STATE_REPOSITORY }}'
    assert worker['env']['GH_STATE_TOKEN'] == '${{ secrets.EDITORIAL_STATE_TOKEN || github.token }}'


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
