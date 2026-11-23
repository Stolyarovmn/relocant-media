import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from relocant_media.ops import actions_runtime as runtime
from relocant_media.ops.belgrade_digests import MORNING_ATTEMPT, due_slot, morning_delivery
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.models import Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('exit_code', [0, 1])
def test_child_early_failure_emits_only_safe_coordinates(capsys, exit_code):
    report = {'status': 'SERVER_FAILED', 'error_type': 'ValueError',
              'error_site': 'server_cycle.py:800', 'message': 'PRIVATE-TIP-TOKEN'}
    program = ("import sys; print('PRIVATE-TIP-TOKEN' * 8000); "
               f"print({json.dumps(report)!r}); sys.exit({exit_code})")
    assert runtime.run_child([sys.executable, '-c', program], dict(os.environ)) == exit_code
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'PRIVATE-TIP-TOKEN' not in captured.err
    if exit_code:
        assert json.loads(captured.err)['runtime_failure'] == {
            'status': 'SERVER_FAILED', 'error_type': 'ValueError',
            'error_site': 'server_cycle.py:800',
        }
    else:
        assert captured.err == ''


def test_child_failure_without_json_keeps_raw_output_hidden(capsys):
    program = "import sys; print('PRIVATE-TIP-TOKEN'); sys.exit(1)"
    assert runtime.run_child([sys.executable, '-c', program], dict(os.environ)) == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ''


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
    store.state['intake_state'] = {'signature': '1,2', 'attempts': 2, 'backup_pending': True}
    def child(command, env):
        assert store.state['status'] == 'busy'
        assert env['EDITORIAL_ACTIONS_CHECKPOINT'] == 'true'
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
    assert 'backup_pending' not in store.state['intake_state']
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


@pytest.mark.parametrize(("minutes", "due"), [(None, True), (10, False), (29, False), (31, True)])
def test_editorial_is_due_once_the_half_hour_interval_has_passed(minutes, due):
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


def test_morning_reason_is_saved_with_the_normal_cycle(context, tmp_path, monkeypatch, capsys):
    store, memory = context
    drive = Mock(spec=runtime.SSHStateClient)
    drive.download.side_effect = memory.download
    drive.upload.side_effect = memory.upload
    drive.checkpoint.return_value = "c" * 64
    slot = due_slot(datetime(2026, 11, 22, 9, tzinfo=UTC))
    monkeypatch.setattr(runtime, "due_digest_slot", lambda _now: slot)
    weather = {"rhmz_forecast": "stale", "meteoalarm": "none_active"}
    digest = AsyncMock(return_value={
        "state": "insufficient_items", "items": 0, "weather_sources": weather,
    })
    monkeypatch.setattr(runtime, "run_digest", digest)
    monkeypatch.setattr(runtime, "run_child", lambda *args: 0)
    env = {
        "ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
        "GITHUB_REF": "refs/heads/main", "GITHUB_ACTIONS": "true",
        "EDITORIAL_SEND_ENABLED": "true", "EDITORIAL_CHANNEL_MODE": "public",
    }
    assert execute((store, drive), tmp_path, monkeypatch, mode="intake", environment=env) == 0
    digest.assert_awaited_once()
    assert store.sha == "initialxx"
    assert "digest_receipts" not in store.state
    assert store.state[MORNING_ATTEMPT] == {"slot": slot.key, "reason": "insufficient_items"}
    assert morning_delivery(store.state, slot.expires).reason == "insufficient_items"
    entries = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["weather_sources"] for row in entries if row.get("digest") == slot.key] == [weather]


def test_morning_watch_output_only_for_an_unconfirmed_public_issue(tmp_path):
    output = tmp_path / "actions-output"
    env = {"GITHUB_OUTPUT": str(output), "GITHUB_ACTIONS": "true",
           "EDITORIAL_SEND_ENABLED": "true", "EDITORIAL_CHANNEL_MODE": "public"}
    now = datetime(2026, 11, 22, 10, tzinfo=UTC)
    state = {"status": "ready", "digest_receipts": {
        "belgrade:2026-11-22:morning": {"state": "sent", "message_id": 152},
    }}
    runtime._write_morning_watch(state, env, now=now)
    state["digest_receipts"].clear()
    runtime._write_morning_watch(state, env, now=now - timedelta(seconds=1))
    runtime._write_morning_watch(state, {**env, "EDITORIAL_SEND_ENABLED": "false"}, now=now)
    assert not output.exists()
    runtime._write_morning_watch(state, env, now=now)
    assert output.read_text() == "morning_watch_date=2026-11-22\n"


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


def test_intake_summary_is_checkpointed_with_only_allowlisted_counters(
        context, tmp_path, monkeypatch):
    def child(command, env):
        database = Path(env['DATABASE_URL'].removeprefix('sqlite:///'))
        (database.parent / 'intake-batch-state.json').write_text(json.dumps({
            'signature': '1', 'attempts': 0,
            'last_result': {
                'status': 'ready', 'checked_at': 'x',
                'intake': {'received': 2, 'telegram_sends': 1, 'secret': 'TOKEN-1'},
                'owner_review': {
                    'status': 'ready',
                    'preparation': {'prepared': 1, 'blocked': 2, 'draft': 'PRIVATE TEXT',
                                    'details': [{'raw_item_id': 7, 'error_type': 'ValueError',
                                                 'reviews': [{'role': 'utility', 'verdict': 'hold',
                                                              'score': 44, 'flags': ['x'],
                                                              'text': 'PRIVATE'}]}]},
                    'queue': {'queued': 3, 'uncertain': 0},
                    'error_type': 'ValueError', 'trace': 'Traceback token=ABC',
                },
                'owner_review_exit_code': 0,
            },
        }), encoding='utf-8')
        return 0
    monkeypatch.setattr(runtime, 'run_child', child)
    assert execute(context, tmp_path, monkeypatch, mode='intake') == 0
    summary = context[0].state['last_intake']
    assert summary == {
        'status': 'ready', 'owner_review_exit_code': 0,
        'intake': {'received': 2, 'telegram_sends': 1},
        'owner_review': {'status': 'ready', 'error_type': 'ValueError',
                         'preparation': {'prepared': 1, 'blocked': 2, 'details': [
                             {'raw_item_id': 7, 'error_type': 'ValueError', 'reviews': [
                                 {'role': 'utility', 'verdict': 'hold', 'score': 44,
                                  'flags': ['x']}]}]},
                         'queue': {'queued': 3, 'uncertain': 0}},
    }
    assert 'TOKEN' not in json.dumps(summary) and 'PRIVATE' not in json.dumps(summary)


def test_editorial_cycle_does_not_erase_the_last_intake_summary(context, tmp_path, monkeypatch):
    context[0].state['last_intake'] = {'status': 'ready', 'owner_review': {'status': 'ready'}}
    monkeypatch.setattr(runtime, 'run_child', lambda command, env: 0)
    assert execute(context, tmp_path, monkeypatch, mode='editorial') == 0
    assert context[0].state['last_intake'] == {
        'status': 'ready', 'owner_review': {'status': 'ready'}}


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
    failure = next(record['runtime_failure'] for line in err.strip().splitlines()
                   if 'runtime_failure' in (record := json.loads(line)))
    assert failure == {'status': 'SERVER_FAILED', 'error_type': 'ValueError',
                       'error_site': 'server_cycle.py:816'}
    assert 'TOKEN-123' not in err


@pytest.mark.parametrize("pipeline_started", [True, False])
def test_intake_pipeline_logs_failure_only_for_current_attempt(
        context, tmp_path, monkeypatch, capsys, pipeline_started):
    def child(command, env):
        directory = Path(env["DATABASE_URL"].removeprefix("sqlite:///")).parent
        (directory / "intake-batch-state.json").write_text(json.dumps({
            "last_result": {"status": "pipeline_failed", "pipeline_started": pipeline_started,
                            "pipeline_exit_code": 1},
        }), encoding="utf-8")
        output = directory / "runs" / "20261117T081300Z"
        output.mkdir(parents=True)
        (output / "server-report.json").write_text(json.dumps({
            "status": "SERVER_FAILED", "error_type": "ValueError",
            "error_site": "server_cycle.py:816", "secret": "PRIVATE-PROPOSAL-TEXT",
        }), encoding="utf-8")
        return 1

    monkeypatch.setattr(runtime, "run_child", child)
    assert execute(context, tmp_path, monkeypatch, mode="intake") == 1
    assert context[0].state["status"] == "ready"
    error = capsys.readouterr().err
    reports = [record["runtime_failure"] for line in error.strip().splitlines()
               if "runtime_failure" in (record := json.loads(line))]
    assert reports == ([{
        "status": "SERVER_FAILED", "error_type": "ValueError", "error_site": "server_cycle.py:816",
    }] if pipeline_started else [])
    assert "PRIVATE-PROPOSAL-TEXT" not in error


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


def test_map_backfill_is_manual_and_does_not_claim_state(context, tmp_path, monkeypatch):
    store, _ = context
    calls = []
    event = 'event_' + 'a' * 20

    async def edit(database, targets, telegram_app, token):
        assert database.is_file()
        calls.append((targets, telegram_app, token))

    monkeypatch.setattr(runtime, 'apply_map_buttons', edit)
    monkeypatch.setattr(runtime, 'run_child', lambda *a: pytest.fail('worker must not run'))
    env = {
        'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
        'GITHUB_REF': 'refs/heads/main', 'EDITORIAL_SEND_ENABLED': 'true',
        'GITHUB_EVENT_NAME': 'workflow_dispatch', 'MAP_BACKFILL_TARGETS': f'96:{event}',
        'RELOCANT_MAP_TELEGRAM_APP': 'https://t.me/relocant_media_belgrade_bot',
        'TELEGRAM_BOT_TOKEN': 'fixture-token',
    }
    options = {'output': tmp_path / 'public.json', 'folder': '', 'mode': 'map-backfill'}
    assert runtime.execute(*context, work=tmp_path / 'allowed', environment=env, **options) == 0
    assert calls == [({96: event}, env['RELOCANT_MAP_TELEGRAM_APP'], 'fixture-token')]
    assert store.sha == 'initial'
    with pytest.raises(ValueError, match='manual editorial delivery'):
        runtime.execute(*context, work=tmp_path / 'blocked',
                        environment={**env, 'GITHUB_EVENT_NAME': 'schedule'}, **options)
    assert len(calls) == 1


def test_map_republish_requires_explicit_hosted_dispatch(context, tmp_path, monkeypatch):
    calls = []
    event = "event_" + "a" * 20
    env = {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
        "GITHUB_REF": "refs/heads/main", "EDITORIAL_SEND_ENABLED": "true",
        "MAP_REPUBLICATION_REQUEST": json.dumps({"message_id": 97, "event_id": event}),
    }
    monkeypatch.setattr(runtime, "apply_map_republication", lambda *args: calls.append(args))
    options = {"output": tmp_path / "public.json", "folder": "", "mode": "map-republish"}
    with pytest.raises(ValueError, match="explicit hosted"):
        runtime.execute(*context, work=tmp_path / "blocked",
                        environment={**env, "GITHUB_EVENT_NAME": "schedule"}, **options)
    assert not calls
    assert runtime.execute(*context, work=tmp_path / "allowed", environment=env, **options) == 0
    assert calls[0][1].message_id == 97 and calls[0][1].event_id == event


def test_map_republish_review_exports_privately_without_a_state_write(
    context, tmp_path, monkeypatch, capsys,
):
    store, drive = context
    ssh = runtime.SSHStateClient(None)
    blobs = {}

    def upload(path, folder):
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        blobs[sha] = json.loads(content)
        return sha

    scope = {"package": {"draft": "private review draft"}}
    monkeypatch.setattr(ssh, "download", drive.download)
    monkeypatch.setattr(ssh, "upload", upload)
    monkeypatch.setattr(runtime, "map_review_request", lambda database, request, previous: scope)
    env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
           "GITHUB_REF": "refs/heads/main", "EDITORIAL_STORAGE_BACKEND": "ssh",
           "MAP_REPUBLICATION_REQUEST": json.dumps(
               {"message_id": 97, "event_id": "event_" + "a" * 20},
           )}
    assert runtime.execute(store, ssh, work=tmp_path / "review", folder="",
                           output=tmp_path / "output.json", mode="map-republish-review",
                           environment=env) == 0
    output = json.loads(capsys.readouterr().out)
    assert set(output) == {"map_republication_review_sha256"}
    assert blobs[output["map_republication_review_sha256"]] == scope
    assert store.sha == "initial"


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
    assert document['jobs']['cycle']['concurrency'] == {
        'group': 'canonical-editorial-state', 'cancel-in-progress': False, 'queue': 'max'}
    steps = document['jobs']['cycle']['steps']
    uploads = [s for s in steps if s.get('uses', '').startswith('actions/upload-')]
    assert len(uploads) == 1 and uploads[0]['with']['path'] == '_site'
    assert 'ACTIONS_EDITORIAL_ENABLED' in document['jobs']['cycle']['if']
    worker = next(step for step in steps if step.get('id') == 'runtime')
    assert worker['env']['GH_STATE_REPOSITORY'] == '${{ vars.EDITORIAL_STATE_REPOSITORY }}'
    assert worker['env']['GH_STATE_TOKEN'] == '${{ secrets.EDITORIAL_STATE_TOKEN || github.token }}'


@pytest.mark.parametrize(("status", "result", "dispatch"), [
    ("in_progress", None, False),
    ("completed", "success", False),
    ("completed", "failure", True),
])
def test_chain_can_check_a_missed_issue_without_waiting_for_cron(
    monkeypatch, status, result, dispatch,
):
    import yaml

    workflow = ROOT / ".github/workflows/editorial-actions.yml"
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    assert jobs["cycle"]["outputs"]["morning_watch_date"] == (
        "${{ steps.runtime.outputs.morning_watch_date }}"
    )
    step = jobs["chain"]["steps"][1]
    assert step["continue-on-error"] is True
    assert "morning_watch_date" in step["if"]
    monkeypatch.setenv("GH_REPO", runtime.REPO)
    monkeypatch.setenv("MORNING_DATE", "2026-11-22")
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_options: json.dumps([
        {"displayTitle": "Утренняя сводка · 2026-11-22", "status": status, "conclusion": result},
    ]))
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    exec(compile(step["run"], str(workflow), "exec"), {})
    if dispatch:
        run.assert_called_once()
        assert run.call_args.args[0][-4:] == [
            "-f", "dry_run=false", "-f", "morning_date=2026-11-22",
        ]
    else:
        run.assert_not_called()


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


def test_intake_projection_reports_only_public_digest_receipts(context, tmp_path, monkeypatch):
    from relocant_media.ops.newsroom_export import snapshot

    observed = '2026-11-23T21:00:00+00:00'
    store, _ = context
    store.state['digest_receipts'] = {
        'belgrade:2026-11-23:morning': {
            'state': 'sent', 'message_id': 163, 'html_sha256': 'PRIVATE',
        },
        'belgrade:2026-11-23:evening': {'state': 'uncertain', 'token': 'PRIVATE'},
    }

    def archived(database, output):
        return {**create_snapshot(database, output), 'created_at': observed}

    def exported(database, output, observed_at, run_dir):
        payload = snapshot(ROOT)
        payload['cities'][0].update(
            runtime_source_count=1, runtime_status='available', observed_at=observed_at,
            daily_brief={'timezone': 'Europe/Belgrade', 'days': [{
                'local_date': '2026-11-23', 'post_count': 0, 'message_ids': [],
                'views': None, 'measured_posts': 0,
            }]},
        )
        output.write_text(json.dumps(payload))

    monkeypatch.setattr(runtime, 'create_snapshot', archived)
    monkeypatch.setattr(runtime, 'export_public', exported)
    monkeypatch.setattr(runtime, 'run_child', lambda *args: 0)
    monkeypatch.setattr(runtime, 'due_digest_slot', lambda *_: None)
    output = tmp_path / 'public.json'
    runtime.execute(*context, work=tmp_path / 'work', output=output,
                    folder='folder_id_1', mode='intake', environment={
                        'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
                        'GITHUB_REF': 'refs/heads/main'})
    saved = output.read_text(encoding='utf-8')
    day, = json.loads(saved)['cities'][0]['daily_brief']['days']
    assert day['morning'] == {'status': 'sent', 'message_id': 163}
    assert day['evening'] == {'status': 'unconfirmed'}
    assert 'PRIVATE' not in saved


def test_capacity_guard_blocks_before_claim_or_child(context, tmp_path, monkeypatch):
    before = context[0].read()
    monkeypatch.setattr(runtime, 'MAX_BYTES', 16 * 1024 * 1024)
    monkeypatch.setattr(runtime, 'run_child', lambda *args: pytest.fail('must not run child'))
    with pytest.raises(runtime.SnapshotCapacityError):
        execute(context, tmp_path, monkeypatch)
    assert context[0].read() == before


def test_capacity_reserve_boundary(tmp_path, monkeypatch):
    database = tmp_path / 'snapshot'
    monkeypatch.setattr(runtime, 'MAX_BYTES', 100)
    monkeypatch.setattr(runtime, 'SNAPSHOT_GROWTH_RESERVE', 20)
    database.write_bytes(b'x' * 80)
    runtime.require_snapshot_capacity(database)
    database.write_bytes(b'x' * 81)
    with pytest.raises(runtime.SnapshotCapacityError):
        runtime.require_snapshot_capacity(database)


@pytest.mark.skipif(sys.platform == "win32", reason="storage helper requires fcntl")
def test_storage_bounds_agree():
    from relocant_media.ops import ssh_state_client, ssh_state_storage
    assert runtime.MAX_BYTES == ssh_state_client.MAX_BLOB == ssh_state_storage.MAX_BLOB
    assert runtime.MAX_BYTES == 256 * 1024 * 1024


def test_projection_failure_commits_receipts_without_public_artifact(
        context, tmp_path, monkeypatch):
    store, drive = context
    monkeypatch.setattr(runtime, 'run_child', lambda *args: 0)
    def broken_projection(database, output, *args):
        output.write_text('PRIVATE partial output')
        raise ValueError('PRIVATE diagnostic')
    monkeypatch.setattr(runtime, 'export_public', broken_projection)
    env = {'ACTIONS_CUTOVER_CONFIRMED': 'true', 'GITHUB_REPOSITORY': runtime.REPO,
           'GITHUB_REF': 'refs/heads/main'}
    assert runtime.execute(store, drive, work=tmp_path / 'work',
        output=tmp_path / 'public.json', folder='folder_id_1', mode='editorial',
        environment=env) == 1
    assert store.state['status'] == 'ready'
    assert store.state['last_exit_code'] == 0
    assert not (tmp_path / 'public.json').exists()
    assert not (tmp_path / 'work/public-projection.json').exists()


def test_private_checkpoint_precedes_projection_and_survives_failed_commit(
        context, tmp_path, monkeypatch):
    store, memory = context
    checkpoints = []
    class PrivateSSH(runtime.SSHStateClient):
        def __init__(self):
            pass
        def download(self, file_id):
            return memory.download(file_id)
        def upload(self, path, folder):
            return memory.upload(path, folder)
        def checkpoint(self, state, expected_sha, path):
            checkpoints.append(copy.deepcopy({'state': state, 'sha': expected_sha}))
            return 'c' * 64
    monkeypatch.setattr(runtime, 'run_child', lambda *args: 0)
    def projection(*args):
        assert len(checkpoints) == 1
        assert checkpoints[0]['sha'] == store.sha
        assert checkpoints[0]['state']['intake_state'] == {'attempts': 2}
        assert checkpoints[0]['state']['last_exit_code'] == 0
        raise ValueError('PRIVATE projection diagnostic')
    monkeypatch.setattr(runtime, 'export_public', projection)
    store.state['intake_state'] = {'attempts': 2}
    store.fail_commit = True
    with pytest.raises(ValueError, match='conflict'):
        runtime.execute(store, PrivateSSH(), work=tmp_path / 'work',
            output=tmp_path / 'public.json', folder='folder_id_1', mode='editorial',
            environment={'ACTIONS_CUTOVER_CONFIRMED': 'true',
                         'GITHUB_REPOSITORY': runtime.REPO, 'GITHUB_REF': 'refs/heads/main'})
    assert store.state['status'] == 'busy'
    assert checkpoints[0]['state']['snapshot']['sha256']
    assert not (tmp_path / 'public.json').exists()


@pytest.mark.parametrize('invalid', [None, {}, 'PRIVATE-TIP-TOKEN'])
def test_failure_processing_errors_ignore_non_lists(capsys, invalid):
    runtime.print_failure_coordinates({'status': 'PRIMARY_FAILED', 'processing_errors': invalid})
    assert json.loads(capsys.readouterr().err) == {
        'runtime_failure': {'status': 'PRIMARY_FAILED'},
    }


def test_failure_processing_coordinates_are_bounded_and_keep_private_fields_hidden(capsys):
    error = {'stage': 'reject', 'error_type': 'ValueError',
             'error_site': 'gpt_fallback.py:123', 'raw_item_id': 123456,
             'message': 'PRIVATE-TIP-TOKEN', 'url': 'https://private.invalid/secret'}
    runtime.print_failure_coordinates({
        'status': 'PRIMARY_FAILED',
        'processing_errors': [None, {'stage': 'PRIVATE TOKEN',
                                    'error_type': 'https://private.invalid/secret'},
                              error, error, error, error, error],
    })
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'PRIVATE' not in captured.err and 'private.invalid' not in captured.err
    assert 'raw_item_id' not in captured.err
    assert json.loads(captured.err)['runtime_failure'] == {
        'status': 'PRIMARY_FAILED',
        'processing_errors': [{'stage': 'reject', 'error_type': 'ValueError',
                               'error_site': 'gpt_fallback.py:123'}] * 3,
    }
