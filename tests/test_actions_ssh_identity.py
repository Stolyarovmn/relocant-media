"""Exercise workflow identity preparation without any remote connection."""
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml


def prepare_step():
    workflow = yaml.load(Path('.github/workflows/editorial-actions.yml').read_text(),
                         Loader=yaml.BaseLoader)
    return next(step for step in workflow['jobs']['cycle']['steps']
                if step.get('id') == 'storage')['run']


def execute(tmp_path, backend, key='', hosts=''):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    python = bin_dir / 'python'
    python.write_text('#!/bin/sh\n'
        '[ "$*" = "-m relocant_media.ops.ssh_state_client --check" ] || exit 3\n'
        'test -f "$EDITORIAL_SSH_KEY_PATH"\n'
        'test -f "$EDITORIAL_SSH_KNOWN_HOSTS_PATH"\n')
    python.chmod(0o700)
    return subprocess.run(['bash', '-c', prepare_step()], env={
        **os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'STORAGE_BACKEND': backend, 'STORAGE_PRIVATE_KEY': key,
        'STORAGE_KNOWN_HOSTS': hosts, 'RUNNER_TEMP': str(tmp_path),
        'GITHUB_OUTPUT': str(tmp_path / 'output')}, capture_output=True, text=True)


def test_real_identity_stays_private_and_is_not_logged(tmp_path):
    original = tmp_path / 'original'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(original)],
                   check=True, capture_output=True)
    key = original.read_text()
    hosts = 'storage.example ' + original.with_suffix('.pub').read_text()
    result = execute(tmp_path, 'ssh', key, hosts)
    assert result.returncode == 0, result.stderr
    directory = tmp_path / 'editorial-storage-identity'
    assert (directory / 'key').read_text().strip() == key.strip()
    assert (directory / 'known_hosts').read_text().strip() == hosts.strip()
    for name in ('key', 'known_hosts'):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert 'PRIVATE KEY' not in result.stdout + result.stderr
    assert key.strip() not in (tmp_path / 'output').read_text()


@pytest.mark.parametrize('backend,key,hosts', [
    ('unknown', '', ''), ('ssh', '', 'host'), ('ssh', 'key', ''),
    ('ssh', 'not-a-key', 'host'),
])
def test_invalid_or_incomplete_identity_stops_before_worker(tmp_path, backend, key, hosts):
    assert execute(tmp_path, backend, key, hosts).returncode != 0


def test_drive_does_not_require_or_write_ssh_credentials(tmp_path):
    assert execute(tmp_path, 'drive').returncode == 0
    assert not (tmp_path / 'editorial-storage-identity').exists()
