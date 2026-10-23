#!/bin/sh
set -eu

APP_DIR="${RELOCANT_APP_DIR:-/opt/relocant-media/app}"
REMOTE_URL="${RELOCANT_GIT_REMOTE_URL:-https://github.com/Stolyarovmn/relocant-media.git}"
BRANCH="${RELOCANT_GIT_BRANCH:-main}"
GITHUB_REPO="${RELOCANT_GITHUB_REPO:-Stolyarovmn/relocant-media}"
WORKER_LOCK="${RELOCANT_WORKER_LOCK:-/var/lib/relocant-media/worker.lock}"
SYSTEMD_DIR="${RELOCANT_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL_BIN="${RELOCANT_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
CURL_BIN="${RELOCANT_CURL_BIN:-/usr/bin/curl}"
PYTHON_BIN="${RELOCANT_PYTHON_BIN:-/usr/bin/python3}"
VENV_PYTHON="${RELOCANT_VENV_PYTHON:-/opt/relocant-media/venv/bin/python}"
ENV_FILE="${RELOCANT_ENV_FILE:-/etc/relocant-media.env}"
TEST_MODE="${RELOCANT_DEPLOY_TEST_MODE:-false}"

if [ "$TEST_MODE" != "true" ] && [ "$(id -u)" -ne 0 ]; then
  echo "production deploy must run as root" >&2
  exit 77
fi

for path in "$APP_DIR" "$SYSTEMD_DIR"; do
  if [ ! -d "$path" ]; then
    echo "required directory is missing: $path" >&2
    exit 78
  fi
done
if [ ! -x "$SYSTEMCTL_BIN" ] || [ ! -x "$CURL_BIN" ] || [ ! -x "$PYTHON_BIN" ] || [ ! -x "$VENV_PYTHON" ]; then
  echo "required deploy executable is missing" >&2
  exit 78
fi

cd "$APP_DIR"
if [ "$(git symbolic-ref --short -q HEAD || true)" != "$BRANCH" ]; then
  echo "production checkout must be on $BRANCH" >&2
  exit 78
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "production checkout is not clean" >&2
  exit 78
fi

# Stop only timers. Never kill a running editorial/intake service: the worker lock
# below waits for a current editorial pass, and active intake makes deployment defer.
"$SYSTEMCTL_BIN" stop relocant-editorial.timer relocant-intake.timer
restart_timers() {
  "$SYSTEMCTL_BIN" start relocant-editorial.timer relocant-intake.timer >/dev/null 2>&1 || true
}
trap restart_timers EXIT INT TERM

exec 9>"$WORKER_LOCK"
if ! /usr/bin/flock -w 2100 9; then
  echo "could not acquire editorial worker lock" >&2
  exit 75
fi
if "$SYSTEMCTL_BIN" is-active --quiet relocant-intake.service; then
  echo "private intake is active; deployment deferred without stopping it" >&2
  exit 75
fi

current="$(git rev-parse --verify HEAD)"
git fetch --no-tags "$REMOTE_URL" "refs/heads/$BRANCH"
target="$(git rev-parse --verify FETCH_HEAD)"

if [ "$current" = "$target" ]; then
  echo "production runtime already matches $BRANCH: $current"
  exit 0
fi
if ! git merge-base --is-ancestor "$current" "$target"; then
  echo "refusing non-fast-forward production deployment: current=$current target=$target" >&2
  exit 78
fi

ci_url="https://api.github.com/repos/$GITHUB_REPO/actions/runs?head_sha=$target&event=push&per_page=20"
ci_json="$("$CURL_BIN" -fsSL -H 'Accept: application/vnd.github+json' "$ci_url")"
ci_state="$(printf '%s' "$ci_json" | "$PYTHON_BIN" -c '
import json, sys
payload = json.load(sys.stdin)
target = sys.argv[1]
for run in payload.get("workflow_runs", []):
    if run.get("name") == "ci" and run.get("head_sha") == target and run.get("event") == "push":
        print("{}:{}".format(run.get("status"), run.get("conclusion")))
        break
else:
    print("missing:missing")
' "$target")"
if [ "$ci_state" != "completed:success" ]; then
  echo "target main is not backed by successful exact-sha CI: $target ($ci_state)" >&2
  exit 78
fi

rollback() {
  echo "deployment failed; restoring checkout $current" >&2
  git reset --hard "$current" >/dev/null 2>&1 || true
  "$VENV_PYTHON" -m pip install -e "$APP_DIR" >/dev/null 2>&1 || true
}

git merge --ff-only "$target"
if ! "$VENV_PYTHON" -m pip install -e "$APP_DIR"; then
  rollback
  exit 1
fi

# Verify the persistent SQLite contract under the same environment used by systemd.
# EnvironmentFile is root-owned deployment configuration and is expected to use
# shell-compatible KEY=VALUE assignments, matching the project's deployed format.
if [ "$TEST_MODE" != "true" ]; then
  if [ ! -r "$ENV_FILE" ]; then
    rollback
    echo "production environment file is not readable" >&2
    exit 78
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  if ! "$VENV_PYTHON" -c '
import os
from sqlalchemy import create_engine
from relocant_media.storage.sqlite_schema import verify_sqlite_schema
engine = create_engine(os.environ["DATABASE_URL"])
try:
    verify_sqlite_schema(engine)
finally:
    engine.dispose()
'; then
    rollback
    echo "new code is incompatible with the current persistent database" >&2
    exit 78
  fi
fi

# Re-check public main before changing installed units. If main advanced during the
# deployment window, return to the previous checkout and let a later deploy take it.
if ! /bin/sh "$APP_DIR/deploy/server/check-main-sync.sh"; then
  rollback
  echo "deployed checkout failed final main-sync verification" >&2
  exit 78
fi

for unit in \
  relocant-editorial.service relocant-editorial.timer \
  relocant-intake.service relocant-intake.timer
do
  /usr/bin/install -m 0644 "$APP_DIR/deploy/server/$unit" "$SYSTEMD_DIR/$unit"
done
"$SYSTEMCTL_BIN" daemon-reload

printf 'deployed green main: %s -> %s\n' "$current" "$target"
