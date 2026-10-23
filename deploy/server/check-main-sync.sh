#!/bin/sh
set -eu

APP_DIR="${RELOCANT_APP_DIR:-/opt/relocant-media/app}"
REMOTE_URL="${RELOCANT_GIT_REMOTE_URL:-https://github.com/Stolyarovmn/relocant-media.git}"
BRANCH="${RELOCANT_GIT_BRANCH:-main}"

cd "$APP_DIR"

git_safe() {
  git -c "safe.directory=$APP_DIR" "$@"
}

actual="$(git_safe rev-parse --verify HEAD)"
if ! git_safe diff-index --quiet HEAD --; then
  echo "production runtime checkout has tracked modifications; refusing editorial cycle" >&2
  exit 78
fi

remote_line="$(git_safe ls-remote --exit-code "$REMOTE_URL" "refs/heads/$BRANCH")"
expected="$(printf '%s\n' "$remote_line" | awk 'NR == 1 {print $1}')"
if [ "${#expected}" -ne 40 ]; then
  echo "could not resolve expected production revision for $BRANCH" >&2
  exit 78
fi

if [ "$actual" != "$expected" ]; then
  echo "production runtime is stale: deployed=$actual expected=$expected; refusing editorial cycle" >&2
  exit 78
fi

printf 'production runtime revision verified: %s\n' "$actual"
