import json
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from relocant_media.ops import intake_watchdog as monitor


def client(health):
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"status": "ready", **({"intake_health": health} if health else {})})))


def test_probe_only_reads_status_and_fails_closed():
    with client({"version": 1, **dict.fromkeys(monitor.FLAGS, False)}) as http:
        assert monitor.inspect("fake-token", client=http) == []
    with client(None) as http:
        assert monitor.inspect("fake-token", client=http) is None
    with client({"version": 1, "queue_waiting": "false"}) as http:
        assert monitor.inspect("fake-token", client=http) == ["unavailable"]


@pytest.mark.asyncio
async def test_incident_deduplicated_and_recovery_once_without_private_data(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    issues = []
    monkeypatch.setattr(monitor, 'inspect', lambda token: list(issues))
    sender = AsyncMock()
    monkeypatch.setattr(monitor, 'send', sender)
    assert (await monitor.check_once(path, dry_run=False))['status'] == 'unchanged'
    issues.append('unavailable')
    assert (await monitor.check_once(path, dry_run=False))['status'] == 'sent'
    await monitor.check_once(path, dry_run=False)
    assert sender.await_count == 1
    issues[:] = ['queue_waiting']
    await monitor.check_once(path, dry_run=False)
    assert sender.await_count == 2
    issues.clear()
    await monitor.check_once(path, dry_run=False)
    await monitor.check_once(path, dry_run=False)
    assert sender.await_count == 3
    assert 'проверка снова проходит' in sender.await_args.args[0]
    assert json.loads(path.read_text()) == {'issues': [], 'delivery': 'sent'}


@pytest.mark.asyncio
async def test_unknown_send_is_not_retried_and_attempt_saved_before_send(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    monkeypatch.setattr(monitor, 'inspect', lambda token: ['uncertain_delivery'])
    calls = []

    async def lost(text):
        calls.append(text)
        assert json.loads(path.read_text())['delivery'] == 'sending'
        raise httpx.ReadTimeout('private error must not be logged')

    monkeypatch.setattr(monitor, 'send', lost)
    assert (await monitor.check_once(path, dry_run=False))['status'] == 'uncertain'
    assert (await monitor.check_once(path, dry_run=False))['status'] == 'unchanged'
    assert len(calls) == 1
    assert 'private error' not in path.read_text()


@pytest.mark.asyncio
async def test_preview_and_older_receiver_never_send_or_modify_cache(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    sender = AsyncMock()
    monkeypatch.setattr(monitor, 'send', sender)
    monkeypatch.setattr(monitor, 'inspect', lambda token: ['unavailable'])
    await monitor.check_once(path, dry_run=True)
    monkeypatch.setattr(monitor, 'inspect', lambda token: None)
    await monitor.check_once(path, dry_run=False)
    assert not path.exists()
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_test_is_labelled_and_does_not_reset_incident(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps({'issues': ['queue_waiting'], 'delivery': 'sent'}))
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    sender = AsyncMock()
    monkeypatch.setattr(monitor, 'send', sender)
    await monitor.check_once(path, dry_run=False, test_notification=True)
    await monitor.check_once(path, dry_run=False, test_notification=True)
    assert sender.await_count == 1
    assert 'Это тест, сбоя нет' in sender.await_args.args[0]
    assert json.loads(path.read_text())['issues'] == ['queue_waiting']


@pytest.mark.asyncio
async def test_only_private_owner_receives_alert(monkeypatch):
    monkeypatch.setenv('EDITORIAL_OWNER_CHAT_ID', '42')
    monkeypatch.setenv('EDITORIAL_OWNER_USER_ID', '43')
    with pytest.raises(ValueError, match='private owner'):
        await monitor.send('test')


def test_monitor_is_independent_from_ssh_editorial_job():
    from pathlib import Path
    workflow = yaml.safe_load(Path('.github/workflows/editorial-watchdog.yml').read_text())
    job = workflow['jobs']['intake']
    assert 'needs' not in job
    assert all('SSH' not in str(step.get('env', {})) for step in job['steps'])
    assert job['steps'][-1]['if'].startswith('always()')
