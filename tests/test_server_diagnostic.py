import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "deploy/server/diagnose-editorial.sh"
HEAD = "a" * 40
MAIN = "b" * 40


def run_diagnostic(tmp_path, *, unavailable=False, same_head=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands"
    git = bin_dir / "git"
    git.write_text("""#!/bin/sh
printf 'git %s\\n' "$*" >> "$DIAGNOSTIC_LOG"
echo 'PRIVATE_CREDENTIAL_IN_STDERR' >&2
case "$*" in
  *rev-parse*HEAD*) printf '%s\\n' "$FAKE_HEAD";;
  *rev-parse*origin/main*) printf '%s\\n' "$FAKE_MAIN";;
  *rev-parse*--git-dir*) echo .git;;
  *diff-index*) exit 1;;
  *show*) echo 'SQLITE_SCHEMA_VERSION = 8'; echo 'SECRET_SOURCE_TEXT';;
  *) exit 98;;
esac
""")
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("""#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$DIAGNOSTIC_LOG"
echo 'SECRET_ENV_FILE_PATH_AND_TOKEN' >&2
[ "$UNAVAILABLE" = true ] && exit 1
cat <<'DATA'
LoadState=loaded
ActiveState=active
SubState=waiting
UnitFileState=enabled
Result=success
ExecMainCode=1
ExecMainStatus=0
ExecMainStartTimestamp=Mon 2026-10-26 09:00:00 UTC
LastTriggerUSec=Mon 2026-10-26 12:00:00 UTC
NextElapseUSecRealtime=n/a
TimersCalendar={ OnCalendar=*-*-* *:00/5:00 UTC ; next_elapse=Mon 2026-10-26 12:05:00 UTC }
TimersMonotonic={ OnBootUSec=3min ; next_elapse=SECRET_MONOTONIC }
Environment=SECRET_ENV_VALUE
ExecStart=https://private.example/SECRET_URL
Description=SECRET_DESCRIPTION
InvalidProperty=SECRET_PRIVATE_NEWS
DATA
""")
    for path in (git, systemctl):
        path.chmod(0o755)
    result = subprocess.run(
        ["sh", str(SCRIPT), "/do/not/read/env"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "DIAGNOSTIC_LOG": str(log), "FAKE_HEAD": HEAD,
             "FAKE_MAIN": HEAD if same_head else MAIN,
             "UNAVAILABLE": "true" if unavailable else "false"},
        capture_output=True, text=True, check=True,
    )
    return result, log.read_text()


def test_diagnostic_is_read_only_and_prints_only_allowlisted_structural_fields(tmp_path):
    result, calls = run_diagnostic(tmp_path)
    assert f"HEAD={HEAD}" in result.stdout
    assert "cached_main_sync=mismatch" in result.stdout
    assert "remote_main=not_contacted" in result.stdout
    assert "tracked_worktree=modified" in result.stdout
    assert "code_expected_sqlite_schema=8" in result.stdout
    assert "database_schema=unknown_not_read" in result.stdout
    assert "OnCalendar=*-*-* *:00/5:00 UTC" in result.stdout
    assert "OnBootUSec=3min" in result.stdout
    assert "ExecMainStartTimestamp=Mon 2026-10-26 09:00:00 UTC" in result.stdout
    assert "SECRET" not in result.stdout + result.stderr
    assert "https://" not in result.stdout
    assert not result.stderr
    assert calls.count("systemctl show ") == 6
    for command in ("fetch", "pull", "reset", "checkout", "restart", "stop", "start ",
                    "daemon-reload", "journalctl", "EnvironmentFiles"):
        assert command not in calls


def test_matching_cache_is_explicitly_not_fresh_remote_observation(tmp_path):
    result, _ = run_diagnostic(tmp_path, same_head=True)
    assert "cached_main_sync=matches_local_cache_only" in result.stdout
    assert "remote_main=not_contacted" in result.stdout


def test_missing_systemd_is_unknown_without_stderr_details(tmp_path):
    result, _ = run_diagnostic(tmp_path, unavailable=True)
    assert result.stdout.count("systemctl_status=unavailable") == 6
    assert "SECRET" not in result.stdout + result.stderr
