#!/bin/sh
set -eu

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_DIR="${RELOCANT_APP_DIR:-/opt/relocant-media/app}"
REMOTE_URL="${RELOCANT_GIT_REMOTE_URL:-https://github.com/Stolyarovmn/relocant-media.git}"
BRANCH="${RELOCANT_GIT_BRANCH:-main}"
GITHUB_REPO="${RELOCANT_GITHUB_REPO:-Stolyarovmn/relocant-media}"
CI_WORKFLOW_PATH="${RELOCANT_CI_WORKFLOW_PATH:-.github/workflows/ci.yml}"
WORKER_LOCK="${RELOCANT_WORKER_LOCK:-/var/lib/relocant-media/worker.lock}"
SYSTEMD_DIR="${RELOCANT_SYSTEMD_DIR:-/etc/systemd/system}"
DEPLOY_LIBEXEC_DIR="${RELOCANT_DEPLOY_LIBEXEC_DIR:-/usr/local/libexec/relocant-media}"
DEPLOY_EXEC="$DEPLOY_LIBEXEC_DIR/deploy-green-main.sh"
SYSTEMCTL_BIN="${RELOCANT_SYSTEMCTL_BIN:-/usr/bin/systemctl}"
CURL_BIN="${RELOCANT_CURL_BIN:-/usr/bin/curl}"
PYTHON_BIN="${RELOCANT_PYTHON_BIN:-/usr/bin/python3}"
VENV_PYTHON="${RELOCANT_VENV_PYTHON:-/opt/relocant-media/venv/bin/python}"
ENV_FILE="${RELOCANT_ENV_FILE:-/etc/relocant-media.env}"
TEST_MODE="${RELOCANT_DEPLOY_TEST_MODE:-false}"
EXPECTED_TARGET="${RELOCANT_EXPECTED_TARGET_SHA:-}"
FORCE_DATABASE_CHECK="${RELOCANT_REPAIR_DATABASE:-false}"

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

git_safe() {
  git -c "safe.directory=$APP_DIR" "$@"
}

if [ "$(git_safe symbolic-ref --short -q HEAD || true)" != "$BRANCH" ]; then
  echo "production checkout must be on $BRANCH" >&2
  exit 78
fi
if [ -n "$(git_safe status --porcelain)" ]; then
  echo "production checkout is not clean" >&2
  exit 78
fi

control_plane_current() {
  [ -x "$DEPLOY_EXEC" ] || return 1
  cmp -s "$APP_DIR/deploy/server/deploy-green-main.sh" "$DEPLOY_EXEC" || return 1
  for unit in \
    relocant-editorial.service relocant-editorial.timer \
    relocant-intake.service relocant-intake.timer \
    relocant-deploy.service relocant-deploy.timer
  do
    [ -f "$SYSTEMD_DIR/$unit" ] || return 1
    cmp -s "$APP_DIR/deploy/server/$unit" "$SYSTEMD_DIR/$unit" || return 1
  done
}

install_control_plane() {
  /usr/bin/install -d -m 0755 "$DEPLOY_LIBEXEC_DIR" || return 1
  /usr/bin/install -m 0755 "$APP_DIR/deploy/server/deploy-green-main.sh" "$DEPLOY_EXEC" || return 1
  for unit in \
    relocant-editorial.service relocant-editorial.timer \
    relocant-intake.service relocant-intake.timer \
    relocant-deploy.service relocant-deploy.timer
  do
    /usr/bin/install -m 0644 "$APP_DIR/deploy/server/$unit" "$SYSTEMD_DIR/$unit" || return 1
  done
  "$SYSTEMCTL_BIN" daemon-reload || return 1
  if ! "$SYSTEMCTL_BIN" enable --now relocant-deploy.timer; then
    echo "warning: green-main deploy timer was installed but could not be enabled" >&2
  fi
}

control_backup=""
snapshot_control_plane() {
  control_backup="$(mktemp -d)"
  for unit in \
    relocant-editorial.service relocant-editorial.timer \
    relocant-intake.service relocant-intake.timer \
    relocant-deploy.service relocant-deploy.timer
  do
    if [ -f "$SYSTEMD_DIR/$unit" ]; then
      cp -p "$SYSTEMD_DIR/$unit" "$control_backup/$unit"
    fi
  done
  if [ -f "$DEPLOY_EXEC" ]; then
    cp -p "$DEPLOY_EXEC" "$control_backup/deploy-green-main.sh"
  fi
}

restore_control_plane() {
  [ -n "$control_backup" ] || return 0
  for unit in \
    relocant-editorial.service relocant-editorial.timer \
    relocant-intake.service relocant-intake.timer \
    relocant-deploy.service relocant-deploy.timer
  do
    if [ -f "$control_backup/$unit" ]; then
      cp -p "$control_backup/$unit" "$SYSTEMD_DIR/$unit" || return 1
    else
      rm -f "$SYSTEMD_DIR/$unit" || return 1
    fi
  done
  if [ -f "$control_backup/deploy-green-main.sh" ]; then
    cp -p "$control_backup/deploy-green-main.sh" "$DEPLOY_EXEC" || return 1
  else
    rm -f "$DEPLOY_EXEC" || return 1
  fi
  "$SYSTEMCTL_BIN" daemon-reload || return 1
}

# Fetch and validate the exact target before touching editorial timers. This makes
# frequent unattended checks cheap and non-disruptive when main has not changed or
# when the newest commit is not yet backed by successful CI.
current="$(git_safe rev-parse --verify HEAD)"
git_safe fetch --no-tags "$REMOTE_URL" "refs/heads/$BRANCH"
target="$(git_safe rev-parse --verify FETCH_HEAD)"
if [ -n "$EXPECTED_TARGET" ]; then
  case "$EXPECTED_TARGET" in
    *[!0-9a-f]* ) echo "expected target must be a full lowercase commit SHA" >&2; exit 78 ;;
  esac
  if [ "${#EXPECTED_TARGET}" -ne 40 ] || [ "$target" != "$EXPECTED_TARGET" ]; then
    echo "main no longer matches the pinned recovery target; retry a verified revision" >&2
    exit 78
  fi
fi

if [ "$current" != "$target" ] && ! git_safe merge-base --is-ancestor "$current" "$target"; then
  echo "refusing non-fast-forward production deployment: current=$current target=$target" >&2
  exit 78
fi
if [ "$current" = "$target" ] && [ "$FORCE_DATABASE_CHECK" != true ] && control_plane_current; then
  echo "production runtime already matches $BRANCH: $current"
  exit 0
fi

ci_url="https://api.github.com/repos/$GITHUB_REPO/actions/runs?head_sha=$target&event=push&per_page=20"
ci_json="$("$CURL_BIN" -fsSL -H 'Accept: application/vnd.github+json' "$ci_url")"
ci_state="$(printf '%s' "$ci_json" | "$PYTHON_BIN" -c '
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
' "$target" "$BRANCH" "$CI_WORKFLOW_PATH")"
if [ "$ci_state" != "completed:success" ]; then
  echo "target main is not backed by successful exact-sha CI: $target ($ci_state)" >&2
  exit 78
fi

# A server may already have the exact green checkout but predate the deploy watcher.
# Install only root-owned deployment control files in that case; do not disturb the
# editorial/intake timers or rerun package/database work.
if [ "$current" = "$target" ] && [ "$FORCE_DATABASE_CHECK" != true ]; then
  snapshot_control_plane
  finish_control_install() {
    status=$?
    trap - EXIT INT TERM
    if [ "$status" -ne 0 ]; then
      if ! restore_control_plane; then
        echo "control plane restore failed; manual recovery required" >&2
        exit "$status"
      fi
    fi
    rm -rf "$control_backup"
    exit "$status"
  }
  trap finish_control_install EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  install_control_plane
  echo "installed green deployment watcher for $BRANCH: $current"
  exit 0
fi

# Stop only timers after a green update is known to exist. Never kill a running
# editorial/intake service: the worker lock below waits for a current editorial pass,
# and active intake makes deployment defer.
"$SYSTEMCTL_BIN" stop relocant-editorial.timer relocant-intake.timer
restart_timers() {
  "$SYSTEMCTL_BIN" start relocant-editorial.timer relocant-intake.timer >/dev/null 2>&1
}
deployment_started=false
migration_receipt="${WORKER_LOCK}.deploy-${target}-$$.json"
rollback() {
  echo "deployment failed; restoring database and checkout $current" >&2
  if [ -f "$migration_receipt" ]; then
    "$VENV_PYTHON" -m relocant_media.ops.server_database_maintenance rollback \
      --receipt "$migration_receipt" || return 1
  fi
  git_safe reset --hard "$current" >/dev/null 2>&1 || return 1
  "$VENV_PYTHON" -m pip install -e "$APP_DIR" >/dev/null 2>&1 || return 1
  restore_control_plane || return 1
}
finish_deployment() {
  status=$?
  trap - EXIT INT TERM
  safe=true
  if [ "$status" -ne 0 ] && [ "$deployment_started" = true ]; then
    if ! rollback; then
      safe=false
      echo "rollback not verified; editorial/intake timers remain stopped" >&2
    fi
  fi
  if [ "$safe" = true ]; then
    if restart_timers; then
      [ -z "$control_backup" ] || rm -rf "$control_backup"
    else
      echo "database/checkout are safe, but timer restart failed; manual recovery required" >&2
      [ "$status" -ne 0 ] || status=1
    fi
  fi
  exit "$status"
}
trap finish_deployment EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

exec 9>"$WORKER_LOCK"
if ! /usr/bin/flock -w 2100 9; then
  echo "could not acquire editorial worker lock" >&2
  exit 75
fi
if "$SYSTEMCTL_BIN" is-active --quiet relocant-intake.service; then
  echo "private intake is active; deployment deferred without stopping it" >&2
  exit 75
fi
# Intake owns this lock while polling as well as while storing updates. Checking
# systemd alone leaves a start race; hold both locks through migration and rollback.
intake_lock="$(dirname "$WORKER_LOCK")/intake-consumer.lock"
exec 8>"$intake_lock"
chown --reference="$WORKER_LOCK" "$intake_lock"
chmod --reference="$WORKER_LOCK" "$intake_lock"
if ! /usr/bin/flock -n 8; then
  echo "private intake consumer is active; deployment deferred" >&2
  exit 75
fi

# Main may advance while we wait for the worker lock. Never deploy an intermediate
# commit and call it current production; defer and let the next watcher pass retry.
latest_line="$(git_safe ls-remote --exit-code "$REMOTE_URL" "refs/heads/$BRANCH")"
latest="$(printf '%s\n' "$latest_line" | awk 'NR == 1 {print $1}')"
if [ -z "$latest" ] || [ "$latest" != "$target" ]; then
  echo "public $BRANCH advanced while deployment was waiting; retry later" >&2
  exit 75
fi

snapshot_control_plane
deployment_started=true
git_safe merge --ff-only "$target"
if ! "$VENV_PYTHON" -m pip install -e "$APP_DIR"; then
  exit 1
fi

# Verify the persistent SQLite contract under the same environment used by systemd.
# EnvironmentFile is root-owned deployment configuration and is expected to use
# shell-compatible KEY=VALUE assignments, matching the project's deployed format.
if [ "$TEST_MODE" != "true" ] || [ "$FORCE_DATABASE_CHECK" = true ]; then
  if [ ! -r "$ENV_FILE" ]; then
    echo "production environment file is not readable" >&2
    exit 78
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  if ! "$VENV_PYTHON" -m relocant_media.ops.server_database_maintenance upgrade \
    --receipt "$migration_receipt"; then
    echo "persistent database verification or known-version upgrade failed" >&2
    exit 78
  fi
fi

# Re-check public main before changing installed units. If main advanced during the
# deployment window, return to the previous checkout and let a later deploy take it.
if ! /bin/sh "$APP_DIR/deploy/server/check-main-sync.sh"; then
  echo "deployed checkout failed final main-sync verification" >&2
  exit 78
fi

# The recurring root service executes only this root-owned installed copy. Never make
# systemd execute a privileged script directly from the application checkout.
install_control_plane

printf 'deployed green main: %s -> %s\n' "$current" "$target"
