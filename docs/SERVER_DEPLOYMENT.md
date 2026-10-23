# Guarded production deployment

The production editorial checkout lives at `/opt/relocant-media/app`. A successful GitHub
merge does **not** by itself update a server that predates the deploy watcher, so one guarded
bootstrap is required before production can keep itself current.

## First guarded bootstrap from a stale checkout

Run the following from a shell that can use `sudo`. It fetches `main` without changing the
working tree, requires a completed successful push run from the canonical
`.github/workflows/ci.yml` for that exact target SHA on `main`, extracts the deploy script from
that already-green commit, and only then executes it as root:

```sh
cd /opt/relocant-media/app
REMOTE_URL=https://github.com/Stolyarovmn/relocant-media.git
GITHUB_REPO=Stolyarovmn/relocant-media
CI_WORKFLOW_PATH=.github/workflows/ci.yml

git fetch --no-tags "$REMOTE_URL" refs/heads/main
TARGET="$(git rev-parse --verify FETCH_HEAD)"
CI_STATE="$(
  /usr/bin/curl -fsSL -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$GITHUB_REPO/actions/runs?head_sha=$TARGET&event=push&per_page=20" \
  | /usr/bin/python3 -c '
import json, sys
payload = json.load(sys.stdin)
target, branch, workflow_path = sys.argv[1:4]
for run in payload.get("workflow_runs", []):
    if (
        run.get("name") == "ci"
        and run.get("path") == workflow_path
        and run.get("head_sha") == target
        and run.get("head_branch") == branch
        and run.get("event") == "push"
    ):
        print("{}:{}".format(run.get("status"), run.get("conclusion")))
        break
else:
    print("missing:missing")
' "$TARGET" main "$CI_WORKFLOW_PATH"
)"
[ "$CI_STATE" = completed:success ]
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT INT TERM
git show "${TARGET}:deploy/server/deploy-green-main.sh" > "$TMP"
sudo env \
  RELOCANT_APP_DIR=/opt/relocant-media/app \
  RELOCANT_GIT_REMOTE_URL="$REMOTE_URL" \
  RELOCANT_GITHUB_REPO="$GITHUB_REPO" \
  RELOCANT_CI_WORKFLOW_PATH="$CI_WORKFLOW_PATH" \
  /bin/sh "$TMP"
```

The bootstrap never starts an editorial cycle and never calls Telegram. A dirty checkout,
non-fast-forward history, missing/failed CI, CI from another workflow or branch, incompatible
database, active private intake or another failed guard stops the deployment. If the checkout
already equals the exact green `main`, the bootstrap only installs the deployment control
plane; it does not stop the editorial/intake timers or reinstall the application package.

## Normal deployment contract

`deploy/server/deploy-green-main.sh` only updates the runtime when all of these are true:

- the checkout is on `main` and completely clean;
- the current checkout can fast-forward to the public GitHub `main`;
- the exact target SHA has a completed successful push run from the canonical `ci.yml` on
  `main`;
- the existing editorial worker lock can be acquired without killing a running cycle;
- private intake is not currently active;
- public `main` has not advanced while the deployment was waiting for the worker lock;
- the editable virtualenv can be refreshed successfully;
- the persistent SQLite database passes the new code's exact schema/integrity checks;
- the deployed checkout still matches public `main` immediately before systemd units change.

A no-op check or a target without successful exact-SHA CI does not stop editorial/intake
timers. Once a green update is known to exist, only those two timers are stopped. A running
editorial service is allowed to finish naturally before the shared worker lock is obtained; a
running private-intake service causes deployment to defer instead of being killed. Timers are
restored on every exit path.

If package installation or database validation fails, the checkout is reset to the previous
SHA and the previous editable package is restored best-effort. No migration is performed by
this deployment command: an incompatible SQLite schema blocks the rollout and requires the
existing explicit maintenance/backup path.

After a successful rollout the script installs the editorial/intake units plus
`relocant-deploy.service` and `relocant-deploy.timer`, reloads systemd, and enables the deploy
timer. The recurring root service does **not** execute code directly from the application
checkout: the verified deploy script is installed root-owned at
`/usr/local/libexec/relocant-media/deploy-green-main.sh`, and systemd executes that copy. This
prevents the unprivileged application checkout from becoming a root command surface.

The watcher checks roughly every ten minutes. It uses the same fast-forward, canonical
exact-SHA CI, worker-lock, database and final-main guards; it does not run an editorial cycle
and it does not call Telegram. When production already matches `main` and the installed
control plane matches the checked-in files, the watcher exits without touching systemd.

For the current production channel this deployment is also the boundary that makes Telegram
presentation changes real. Current `main` hides the raw final source URL behind the clickable
`Источник` entity and asks Telegram for a large preview of the same verified primary-source
URL. Older server checkouts can therefore visibly produce long links/no preview even when the
repository has already fixed that behavior.
