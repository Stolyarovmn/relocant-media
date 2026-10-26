#!/bin/sh
# Read-only server diagnostics. No network, environment files, DB, journals or Telegram.
set -u
export LC_ALL=C TZ=UTC GIT_OPTIONAL_LOCKS=0

app_dir="${1:-/opt/relocant-media/app}"

git_read() {
  git -C "$app_dir" -c "safe.directory=$app_dir" -c core.fsmonitor=false "$@" 2>/dev/null
}

safe_sha() {
  case "$1" in ''|*[!0-9a-f]*) printf '%s' unknown;;
    *) if [ "${#1}" -eq 40 ] || [ "${#1}" -eq 64 ]; then
         printf '%s' "$1"
       else printf '%s' unknown; fi;;
  esac
}

head_sha="$(safe_sha "$(git_read rev-parse --verify HEAD || true)")"
main_sha="$(safe_sha "$(git_read rev-parse --verify refs/remotes/origin/main || true)")"
printf 'diagnostic_mode=read_only_offline\nHEAD=%s\ncached_origin_main=%s\n' "$head_sha" "$main_sha"
printf 'remote_main=not_contacted\n'
if [ "$head_sha" = unknown ] || [ "$main_sha" = unknown ]; then
  printf 'cached_main_sync=unknown\n'
elif [ "$head_sha" = "$main_sha" ]; then
  printf 'cached_main_sync=matches_local_cache_only\n'
else
  printf 'cached_main_sync=mismatch\n'
fi
if git_read rev-parse --git-dir >/dev/null; then
  git_read diff-index --quiet HEAD -- >/dev/null
  diff_status=$?
  case "$diff_status" in 0) tracked_state=clean;; 1) tracked_state=modified;; *) tracked_state=unknown;; esac
else
  tracked_state=unknown
fi
printf 'tracked_worktree=%s\n' "$tracked_state"
# Only the numeric expected version in committed code is disclosed. No database is opened.
expected_schema="$(git_read show HEAD:src/relocant_media/storage/sqlite_schema.py |
  awk '$1 == "SQLITE_SCHEMA_VERSION" && $2 == "=" && $3 ~ /^[0-9]+$/ {print $3; exit}')"
case "$expected_schema" in ''|*[!0-9]*) expected_schema=unknown;; esac
printf 'code_expected_sqlite_schema=%s\ndatabase_schema=unknown_not_read\n' "$expected_schema"

for unit in relocant-editorial.service relocant-editorial.timer relocant-intake.service \
  relocant-intake.timer relocant-deploy.service relocant-deploy.timer; do
  printf '\nunit=%s\n' "$unit"
  if properties="$(systemctl show "$unit" --no-pager \
    --property=LoadState,ActiveState,SubState,UnitFileState,Result,ExecMainCode,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp,LastTriggerUSec,NextElapseUSecRealtime,TimersCalendar,TimersMonotonic \
    2>/dev/null)"; then
    printf '%s\n' "$properties" | awk '
      index($0, "=") {
        key = substr($0, 1, index($0, "=") - 1)
        value = substr($0, index($0, "=") + 1)
        if (key ~ /^(LoadState|ActiveState|SubState|UnitFileState|Result)$/) {
          if (value ~ /^(loaded|not-found|masked|error|bad-setting|merged|stub|active|reloading|inactive|failed|activating|deactivating|maintenance|refreshing|dead|running|exited|waiting|elapsed|start|stop|start-pre|start-post|stop-sigterm|stop-sigkill|auto-restart|enabled|enabled-runtime|disabled|static|indirect|generated|transient|linked|linked-runtime|alias|success|exit-code|signal|core-dump|timeout|watchdog|start-limit-hit|resources|protocol|dependency|oom-kill|condition)$/)
            print key "=" value
          else print key "=unknown"
        } else if (key ~ /^(ExecMainCode|ExecMainStatus)$/) {
          if (value ~ /^[0-9]+$/) print key "=" value
          else print key "=unknown"
        } else if (key ~ /^(ExecMainStartTimestamp|ExecMainExitTimestamp|LastTriggerUSec|NextElapseUSecRealtime)$/) {
          if (value == "" || value == "n/a") print key "=not_observed"
          else if (value ~ /^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9] UTC$/)
            print key "=" value
          else print key "=unrecognized"
        } else if (key == "TimersCalendar") {
          if (value == "") { print key "=not_observed"; next }
          sub(/^.*OnCalendar=/, "", value)
          sub(/ ;.*$/, "", value)
          if (value ~ /^[0-9*:,./ -]+ UTC$/) print "OnCalendar=" value
          else print key "=unrecognized"
        } else if (key == "TimersMonotonic") {
          count = split(value, intervals, ";")
          for (i = 1; i <= count; i++) {
            item = intervals[i]
            sub(/^[ {]+/, "", item)
            sub(/ +$/, "", item)
            pair_count = split(item, pair, "=")
            if (pair_count == 2 && pair[1] ~ /^(OnBootUSec|OnStartupUSec|OnActiveUSec|OnUnitActiveUSec|OnUnitInactiveUSec)$/ && pair[2] ~ /^([0-9]+(us|ms|s|min|h|d) ?)+$/)
              print pair[1] "=" pair[2]
          }
        }
      }'
  else
    printf 'systemctl_status=unavailable\n'
  fi
done
printf '\ninterpretation=service_success_is_not_delivery_confirmation\n'
