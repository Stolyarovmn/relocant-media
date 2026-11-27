#!/bin/sh
set -eu

PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_DIR="${RELOCANT_APP_DIR:-/opt/relocant-media/app}"
REMOTE_URL="${RELOCANT_GIT_REMOTE_URL:-https://github.com/Stolyarovmn/relocant-media.git}"
BRANCH="${RELOCANT_GIT_BRANCH:-main}"
GITHUB_REPO="${RELOCANT_GITHUB_REPO:-Stolyarovmn/relocant-media}"
CI_WORKFLOW_PATH="${RELOCANT_CI_WORKFLOW_PATH:-.github/workflows/ci.yml}"
WORKER_LOCK="${RELOCANT_WORKER_LOCK:-/var/lib/relocant-locks/worker.lock}"
LOCK_DIR="$(dirname "$WORKER_LOCK")"
INTAKE_LOCK="$LOCK_DIR/intake-consumer.lock"
DEPLOY_STATE_DIR="${RELOCANT_DEPLOY_STATE_DIR:-/var/lib/relocant-deploy}"
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

if [ "$TEST_MODE" != "true" ]; then
  if [ "$WORKER_LOCK" != /var/lib/relocant-locks/worker.lock ] ||
     [ "$DEPLOY_STATE_DIR" != /var/lib/relocant-deploy ]; then
    echo "deployment requires the installed shared locks and deploy state paths" >&2
    exit 78
  fi
  # Use the system interpreter before trusting any executable from the deployment.
  /usr/bin/python3 -I - "$APP_DIR" "$VENV_PYTHON" "$ENV_FILE" "$SYSTEMD_DIR" \
    "$DEPLOY_LIBEXEC_DIR" "$DEPLOY_STATE_DIR" "$WORKER_LOCK" "$INTAKE_LOCK" \
    "$SYSTEMCTL_BIN" "$CURL_BIN" "$PYTHON_BIN" <<'PYTHON'
from collections import deque
import grp
import os
from pathlib import Path
import stat
import sys


def trusted_path(value, *, optional=False, shared_lock=False):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"expected a canonical absolute path: {value}")
    parts = deque(path.parts[1:])
    current = Path("/")
    links = 0
    while parts:
        component = parts.popleft()
        if component == "..":
            current = current.parent
            continue
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if optional and not parts:
                return current
            raise
        is_link = stat.S_ISLNK(info.st_mode)
        final_lock = shared_lock and not parts
        write_mask = 0o002 if final_lock else 0o022
        if info.st_uid != 0 or (not is_link and info.st_mode & write_mask):
            raise ValueError(f"untrusted owner or write permissions: {current}")
        if final_lock and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise ValueError(f"shared lock must be a regular file with one name: {current}")
        if is_link:
            links += 1
            if links > 40:
                raise ValueError(f"too many symbolic links: {value}")
            target = Path(os.readlink(current))
            if target.is_absolute():
                parts.extendleft(reversed(target.parts[1:]))
                current = Path("/")
            else:
                parts.extendleft(reversed(target.parts))
                current = current.parent
    return current


def trusted_tree(root):
    def failed(error):
        raise error

    for folder, directories, files in os.walk(root, onerror=failed):
        for name in (*directories, *files):
            entry = Path(folder) / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = trusted_path(entry)
                if target.is_dir() and not target.is_relative_to(root):
                    raise ValueError(f"directory link leaves trusted tree: {entry}")
            elif info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError(f"untrusted owner or write permissions: {entry}")
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"unexpected file type: {entry}")


try:
    app, venv_python, env_file, systemd, libexec, state, worker, intake, *tools = sys.argv[1:]
    checkout = trusted_path(app)
    if not (checkout / ".git").is_dir() or (checkout / ".git").is_symlink():
        raise ValueError("production checkout requires an in-place Git directory")
    trusted_tree(checkout)
    trusted_tree(trusted_path(Path(venv_python).parent.parent))
    for path in (venv_python, env_file, *tools):
        if not trusted_path(path).is_file():
            raise ValueError(f"expected a regular file: {path}")
    if not trusted_path(systemd).is_dir():
        raise ValueError(f"expected a directory: {systemd}")
    for path in (libexec, state):
        candidate = trusted_path(path, optional=True)
        if candidate.exists() and (candidate != Path(path) or not candidate.is_dir()):
            raise ValueError(f"expected an in-place deploy directory: {path}")
    receipt_dir = Path(state)
    if receipt_dir.exists() and stat.S_IMODE(receipt_dir.stat().st_mode) != 0o700:
        raise ValueError(f"deploy state directory must have mode 0700: {state}")
    runtime_group = grp.getgrnam("relocant").gr_gid
    lock_parent = Path(worker).parent
    parent_info = trusted_path(lock_parent).stat()
    if parent_info.st_gid != runtime_group or stat.S_IMODE(parent_info.st_mode) != 0o750:
        raise ValueError(f"shared lock directory must be root:relocant mode 0750: {lock_parent}")
    for path in (worker, intake):
        info = trusted_path(path, shared_lock=True).stat()
        if info.st_gid != runtime_group or stat.S_IMODE(info.st_mode) != 0o660:
            raise ValueError(f"shared lock must be root:relocant mode 0660: {path}")
    for path in (receipt_dir / "deploy.lock", Path(libexec) / "deploy-green-main.sh",
                 *(Path(systemd) / f"relocant-{name}.{kind}"
                   for name in ("editorial", "intake", "deploy") for kind in ("service", "timer"))):
        checked = trusted_path(path, optional=True) if path.parent.exists() else path
        if checked != path or (path.exists() and not path.is_file()):
            raise ValueError(f"expected an in-place deploy file: {path}")
except (KeyError, OSError, ValueError) as error:
    print(f"deployment path validation failed: {error}", file=sys.stderr)
    sys.exit(78)
PYTHON
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

umask 077
/usr/bin/install -d -m 0700 "$DEPLOY_STATE_DIR"
exec 7>>"$DEPLOY_STATE_DIR/deploy.lock"
if ! /usr/bin/flock -n 7; then
  echo "another deployment is active" >&2
  exit 75
fi
umask 022
cd "$APP_DIR"

if [ "$(git symbolic-ref --short -q HEAD || true)" != "$BRANCH" ]; then
  echo "production checkout must be on $BRANCH" >&2
  exit 78
fi
if ! checkout_status="$(git status --porcelain)" || [ -n "$checkout_status" ]; then
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
  control_backup="$(mktemp -d "$DEPLOY_STATE_DIR/control.XXXXXXXX")"
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
current="$(git rev-parse --verify HEAD)"
git fetch --no-tags "$REMOTE_URL" "refs/heads/$BRANCH"
target="$(git rev-parse --verify FETCH_HEAD)"
if [ -n "$EXPECTED_TARGET" ]; then
  case "$EXPECTED_TARGET" in
    *[!0-9a-f]* ) echo "expected target must be a full lowercase commit SHA" >&2; exit 78 ;;
  esac
  if [ "${#EXPECTED_TARGET}" -ne 40 ] || [ "$target" != "$EXPECTED_TARGET" ]; then
    echo "main no longer matches the pinned recovery target; retry a verified revision" >&2
    exit 78
  fi
fi

if [ "$current" != "$target" ] && ! git merge-base --is-ancestor "$current" "$target"; then
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
migration_receipt="$DEPLOY_STATE_DIR/deploy-${target}-$$.json"
rollback() {
  echo "deployment failed; restoring database and checkout $current" >&2
  if [ -f "$migration_receipt" ]; then
    "$VENV_PYTHON" -m relocant_media.ops.server_database_maintenance rollback \
      --receipt "$migration_receipt" --lock-dir "$LOCK_DIR" || return 1
  fi
  git reset --hard "$current" >/dev/null 2>&1 || return 1
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

if [ "$TEST_MODE" = "true" ]; then
  touch "$WORKER_LOCK" "$INTAKE_LOCK"
fi
exec 9<"$WORKER_LOCK"
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
exec 8<"$INTAKE_LOCK"
if ! /usr/bin/flock -n 8; then
  echo "private intake consumer is active; deployment deferred" >&2
  exit 75
fi

# Main may advance while we wait for the worker lock. Never deploy an intermediate
# commit and call it current production; defer and let the next watcher pass retry.
latest_line="$(git ls-remote --exit-code "$REMOTE_URL" "refs/heads/$BRANCH")"
latest="$(printf '%s\n' "$latest_line" | awk 'NR == 1 {print $1}')"
if [ -z "$latest" ] || [ "$latest" != "$target" ]; then
  echo "public $BRANCH advanced while deployment was waiting; retry later" >&2
  exit 75
fi

snapshot_control_plane
deployment_started=true
git merge --ff-only "$target"
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
    --receipt "$migration_receipt" --lock-dir "$LOCK_DIR"; then
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
