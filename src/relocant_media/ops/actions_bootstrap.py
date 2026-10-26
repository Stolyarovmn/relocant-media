"""Explicit one-time cutover from a final verified Drive backup; never resets busy state."""
from __future__ import annotations

import argparse
import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from relocant_media.ops.actions_runtime import (
    REPO,
    STATE_BRANCH,
    STATE_PATH,
    Drive,
    StateStore,
    bootstrap_record,
    restored,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--final-manifest', type=Path, required=True)
    parser.add_argument('--intake-state', type=Path, required=True,
                        help='captured retry state from the stopped old runtime')
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--confirm-all-former-writers-fenced', action='store_true', required=True)
    args = parser.parse_args()
    try:
        if not args.confirm_all_former_writers_fenced:
            raise ValueError('cutover confirmation required')
        state = bootstrap_record(json.loads(args.final_manifest.read_text()), now=datetime.now(UTC))
        intake = json.loads(args.intake_state.read_text())
        state['intake_state'] = {key: intake[key] for key in
                                 ('signature', 'attempts', 'backup_pending') if key in intake}
        args.work.mkdir(parents=True, exist_ok=False)
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            store = StateStore(client, os.environ['GH_STATE_TOKEN'],
                               repository=os.environ.get('GH_STATE_REPOSITORY') or REPO)
            metadata = store.require_private()
            # A known state of ANY status is never replaced by bootstrap.
            existing = client.get(store.url, params={'ref': STATE_BRANCH}, headers=store.headers)
            if existing.status_code != 404:
                raise ValueError('existing or unreadable canonical state; bootstrap refused')
            restored(Drive(client), state['snapshot'], args.work)
            base = store.base_url
            branch = client.get(f'{base}/git/ref/heads/{STATE_BRANCH}', headers=store.headers)
            if branch.status_code == 404:
                default_branch = metadata.get('default_branch')
                if not isinstance(default_branch, str) or not default_branch:
                    raise ValueError('initialized private state repository required')
                base_ref = client.get(f'{base}/git/ref/heads/{quote(default_branch, safe="")}',
                                      headers=store.headers)
                base_ref.raise_for_status()
                store.require_private()
                created = client.post(f'{base}/git/refs', headers=store.headers,
                    json={'ref': f'refs/heads/{STATE_BRANCH}',
                          'sha': base_ref.json()['object']['sha']})
                created.raise_for_status()
            else:
                branch.raise_for_status()
            store.require_private()
            response = client.put(f'{base}/contents/{STATE_PATH}', headers=store.headers,
                json={'message': 'Initialize canonical Actions state after fenced cutover',
                      'branch': STATE_BRANCH,
                      'content': base64.b64encode(json.dumps(state).encode()).decode()})
            response.raise_for_status()
            verified, _ = store.read()
            if verified != state:
                raise ValueError('bootstrap readback differs')
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({'status': 'initialized', 'runtime_enabled': False}))


if __name__ == '__main__':
    main()
