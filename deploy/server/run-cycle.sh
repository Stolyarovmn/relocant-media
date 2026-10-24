#!/bin/sh
set -eu

output="/var/lib/relocant-media/runs/$(date -u +%Y%m%dT%H%M%S.%NZ)"
lock="/var/lib/relocant-media/worker.lock"
python="/opt/relocant-media/venv/bin/python"

exec 9>"$lock"
if ! /usr/bin/flock -w 2100 9; then
  echo "could not acquire editorial worker lock" >&2
  exit 75
fi

status=0
"$python" -m relocant_media.ops.server_runtime --output "$output" "$@" || status=$?

if [ "$status" -eq 0 ] && [ "${JEV_ENABLED:-false}" = "true" ]; then
  if ! "$python" -m relocant_media.ops.newsroom_status \
    --database \
    --city belgrade \
    --limit 100 \
    --shadow-duplicates \
    > "$output/jev-shadow-duplicates.json"
  then
    printf '%s\n' \
      '{"mode":"shadow_only","decision_effect":"none","status":"unavailable"}' \
      > "$output/jev-shadow-duplicates.json"
  fi
fi

exit "$status"
