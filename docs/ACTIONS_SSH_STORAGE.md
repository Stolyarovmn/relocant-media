# Actions compute, server storage only

Owner architecture: all collection, discovery, model requests, translation, enrichment,
reviews, Telegram polling/delivery, report generation and Pages projection run on
GitHub-hosted runners. The 1 CPU / 1 GB server stores state only. It must not host a
self-hosted runner or a resident editorial/intake process. Google Drive remains an
alternative backend for a later storage migration.

The same `Editorial Actions` workflow and the same editorial services are reused.
`EDITORIAL_STORAGE_BACKEND=ssh` selects storage; the default `drive` remains compatible
with existing configuration. This is not a second newsroom or queue.

## What runs where

| Component | Location |
| --- | --- |
| Sources, candidate selection, models, reviews, translation | GitHub-hosted runner |
| Telegram intake, owner review, channel delivery | GitHub-hosted runner |
| SQLite processing and verification during each cycle | Ephemeral runner workspace |
| Durable immutable snapshots and private ready/busy pointer | Storage server |
| Public sanitized projection | GitHub data branch and Pages |

`server_runtime` is the existing worker module name, not a requirement to run it on
the server. The Actions subprocess invokes it locally on the hosted runner.

## Schedule redundancy

GitHub drops or delays scheduled runs under load. The workflow therefore registers
editorial at :17 and :47 and intake on two disjoint minute sets. Extra slots add no
writer: the single concurrency group serializes them and the cycle windows are
idempotent, so a duplicate slot only finds nothing new to process.

## Self-chaining

Scheduled runs are best effort (one slot arrived in about three hours). With the variable
`ACTIONS_CHAIN_ENABLED=true`, a healthy `cycle` books the next run in mode `auto` (two minutes
later; the concurrency group keeps one pending run, so nothing piles up). `auto` runs editorial when the last checkpointed editorial is
older than 30 minutes, otherwise intake. A failed cycle (for example the exact-main-CI guard right after a merge) pauses the chain for ten minutes
and it continues; cron slots are the extra restart path. Set the variable to anything else to stop chaining. Still one canonical writer.

## Storage guarantees

The standalone `ssh_state_storage.py` helper uses only Python's standard library.
It does not import the application, open SQLite, execute commands from requests,
call models or contact Telegram. Blob transfers are bounded and streamed on the
server. Requests cannot select server paths. Files and state are private; publication
of the existing sanitized projection happens only after a confirmed checkpoint.

One private pointer lives at `/var/lib/relocant-media-state/current.json`; immutable
snapshots live under its `blobs/` directory. SSH uses a dedicated non-root account
and a forced-command key. No private GitHub control repository is required for SSH.
The legacy manifest field `drive_file_id` contains the blob SHA256 for this backend;
it remains internal compatibility data, not a Drive dependency.

The existing workflow concurrency group and atomic compare-and-swap claim protect
one writer. The server accepts ready→busy and matching busy→ready transitions.
Busy never expires. A lost runner or unknown outcome requires reconciliation;
no automatic reset, delivery retry or stale-snapshot failover is introduced.

## Install the storage endpoint (administrative server session)

Use the helper from the exact CI-green `main` revision being deployed. Install it
as root at `/usr/local/libexec/relocant-media/ssh-state-storage` with mode 0755 and
root ownership. It is a standalone Python file; no project dependencies, Node,
Codex CLI or editorial application install is needed to serve storage.

Create a dedicated account, for example `relocant-state`, with `/bin/sh` as its shell
and a root-owned home `/var/lib/relocant-media-ssh`. Give only this account ownership
of `/var/lib/relocant-media-state` (0700). Keep its home and `.ssh` root-owned and
not writable by the storage account (0755 is sufficient); `authorized_keys` is
root-owned 0644 and contains only a dedicated Actions public key with these options:

```text
restrict,command="/usr/bin/python3 -I /usr/local/libexec/relocant-media/ssh-state-storage" ssh-ed25519 ACTIONS_PUBLIC_KEY
```

Do not use an engineering/root key or grant a general remote shell. Validate the
server host key through the already trusted administrative connection. Store that
pinned known_hosts entry in GitHub; do not trust an unauthenticated runner-side
`ssh-keyscan` as proof of server identity.

Actions variables:

- `EDITORIAL_STORAGE_BACKEND=ssh`
- `EDITORIAL_SSH_HOST` — storage hostname or IP
- `EDITORIAL_SSH_USER=relocant-state`
- `EDITORIAL_SSH_PORT=22` (or actual SSH port)

Actions secrets: `EDITORIAL_SSH_PRIVATE_KEY`, `EDITORIAL_SSH_KNOWN_HOSTS`, plus the
existing model/Bot credentials used by the worker. SSH credentials are written to
private runner-temporary files and are not passed to the editorial child process.
`EDITORIAL_STATE_TOKEN` and Drive credentials are not needed for this backend.
No secrets are created or changed automatically.

## Cutover of the existing queue

1. Install the endpoint and configure Actions authentication while the legacy writer
   still owns the queue. A manual preview first performs a storage health check;
   `bootstrapped=false` is expected before cutover. It is not a healthy editorial
   preview yet. A connection error must be resolved before stopping old writers.
2. In the administrative server session, stop/disable the editorial, intake and deploy
   timers. Wait for active services and all external writers to finish; never kill
   an active Telegram send. Take the canonical worker/intake locks before the final
   snapshot. Preserve the current database, offsets, receipts and intake retry state.
3. Fence all three legacy services against reactivation. For locally installed unit
   files, a persistent root-owned drop-in with an unsatisfied `ConditionPathExists`
   is an option; verify the condition path is absent and reload systemd. Do not merely
   disable a timer: the old deployment watcher can re-enable it. Also stop any external
   process writing the same queue. This is actual operational fencing, not a JSON flag.
4. Create the final consistent SQLite backup/manifest and preserve
   `intake-batch-state.json`. Confirm integrity/FK, SHA256 and receipts in isolation.
   Final snapshot age for bootstrap must be at most one hour.
5. Only after those checks, create root-owned `/etc/relocant-media-state-cutover.json`,
   readable by the storage account and not group/other writable. Its contents bind
   the fencing attestation to that exact final snapshot:

```json
{"version":1,"server_writers_fenced":true,"snapshot_sha256":"FINAL_SHA256","snapshot_bytes":12345}
```

6. In a trusted non-server environment with the final snapshot/manifest/intake state
   and the same SSH settings, run the existing bootstrap (no Telegram calls):

```sh
EDITORIAL_STORAGE_BACKEND=ssh python -m relocant_media.ops.actions_bootstrap \
  --final-manifest /secure/final/manifest.json \
  --final-database /secure/final/editorial.sqlite3 \
  --intake-state /secure/intake-batch-state.json \
  --work /secure/new-cutover-check --confirm-all-former-writers-fenced
```

Bootstrap verifies the final database locally, uploads/reads back the snapshot and
creates the private pointer only if absent. It never replaces existing ready/busy
state. It does not enable workflow schedules or delivery.

7. Run Actions `mode=preview` on CI-green main. Confirm the real queue/receipt snapshot
   and current Pages projection. Then enable `ACTIONS_CUTOVER_CONFIRMED` and
   `ACTIONS_EDITORIAL_ENABLED`, preserving the intended delivery/intake/review flags.
   Observe the next natural editorial/intake runs and their checkpoints/receipts.

The result is complete only when the operational cycle is visible in Actions,
server compute is fenced, and the public projection is fresh. A green CI, health
check or storage upload alone does not establish a working newsroom. Do not return
ownership to an old server database after Actions has consumed updates or delivered
messages. Reconcile the newest canonical state first.
