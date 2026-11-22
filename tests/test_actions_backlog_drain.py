"""Drain использует тот же CAS, закрытый снимок и ограниченное продолжение."""

import copy
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from test_actions_runtime import context as _context
from test_actions_runtime import state as _state

from relocant_media.ops import actions_runtime as runtime

state = _state
context = _context
ROOT = Path(__file__).resolve().parents[1]
BEFORE = "2026-11-19T00:00:00+00:00"


def environment(tmp_path, **changes):
    return {
        "ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
        "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_ACTIONS": "true", "DRAIN_BEFORE": BEFORE,
        "DRAIN_MAX_ATTEMPTS": "10", "DRAIN_MAX_SECONDS": "300", "DRAIN_RUNS_LEFT": "3",
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "GH_STATE_TOKEN": "PRIVATE_STATE_TOKEN", "DRIVE_CREDENTIALS_JSON": "PRIVATE_DRIVE",
        "EDITORIAL_SEND_ENABLED": "true", "EDITORIAL_PRIVATE_INTAKE_ENABLED": "true",
        "EDITORIAL_PRIVATE_FEEDBACK_ENABLED": "true", "EDITORIAL_OWNER_REVIEW_ENABLED": "true",
    } | changes


def execute(context, tmp_path, monkeypatch, env=None, *, mode="drain"):
    monkeypatch.setattr(runtime, "export_public", lambda *args: None)
    return runtime.execute(*context, work=tmp_path / "work", output=tmp_path / "public.json",
        folder="folder_id_1", mode=mode, environment=env or environment(tmp_path))


def write_report(command, **changes):
    directory = Path(command[command.index("--output")+1])
    directory.mkdir()
    report = dict(status="DRAIN_OK", remaining=7, needs_continue=True,
                  next_delay_seconds=60, raw_ids=[111, 222], private="PRIVATE_REPORT") | changes
    (directory / "drain-report.json").write_text(json.dumps(report), encoding="utf-8")
    return directory


def test_drain_claims_sweeps_checkpoints_then_exports_only_bounded_control(
    context, tmp_path, monkeypatch, capsys,
):
    store, _ = context
    events = []
    sweep = runtime._sweep_material_lifecycle

    def swept(database):
        assert store.state["status"] == "busy"
        events.append("sweep")
        sweep(database)

    monkeypatch.setattr(runtime, "_sweep_material_lifecycle", swept)

    def child(command, env):
        assert store.state["status"] == "busy"
        assert command[2] == "relocant_media.ops.backlog_drain"
        assert command[command.index("--before")+1] == BEFORE
        assert command[command.index("--max-attempts")+1] == "10"
        assert command[command.index("--max-seconds")+1] == "300"
        assert command[command.index("--runs-left")+1] == "3"
        assert "--send-enabled" in command
        assert "GH_STATE_TOKEN" not in env and "DRIVE_CREDENTIALS_JSON" not in env
        for key in ("EDITORIAL_PRIVATE_INTAKE_ENABLED", "EDITORIAL_PRIVATE_FEEDBACK_ENABLED",
                    "EDITORIAL_OWNER_REVIEW_ENABLED"):
            assert env[key] == "false"
        assert "GITHUB_OUTPUT" not in env and not (tmp_path / "github-output").exists()
        events.append("child")
        write_report(command)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    assert execute(context, tmp_path, monkeypatch) == 0
    assert events == ["sweep", "child", "sweep"]
    assert store.state["status"] == "ready" and store.state["last_exit_code"] == 0
    assert (tmp_path / "work/saved/editorial.sqlite3").is_file()
    assert (tmp_path / "github-output").read_text() == (
        "drain_continue=true\ndrain_next_delay_seconds=60\n")
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert "raw_ids" not in captured.out + captured.err
    assert "remaining" not in (tmp_path / "github-output").read_text()


@pytest.mark.parametrize("changes", [
    {"DRAIN_BEFORE": ""}, {"DRAIN_BEFORE": "2026-11-19T00:00:00"},
    {"DRAIN_BEFORE": "2026-11-19T00:00:00+03:00"},
    {"DRAIN_BEFORE": "2999-11-19T00:00:00Z"},
    {"DRAIN_MAX_ATTEMPTS": "31"}, {"DRAIN_MAX_ATTEMPTS": "0"},
    {"DRAIN_MAX_ATTEMPTS": "1.0"}, {"DRAIN_MAX_ATTEMPTS": "1\nraw_id=9"},
    {"DRAIN_MAX_SECONDS": "901"}, {"DRAIN_MAX_SECONDS": "-1"},
    {"DRAIN_RUNS_LEFT": "201"}, {"DRAIN_RUNS_LEFT": "0"},
    {"GITHUB_EVENT_NAME": "schedule"}, {"GITHUB_ACTIONS": "false"},
])
def test_invalid_drain_parameters_fail_before_restore_or_claim(
    context, tmp_path, monkeypatch, changes,
):
    monkeypatch.setattr(context[0], "read",
                        lambda: pytest.fail("Нет чтения состояния до валидации"))
    monkeypatch.setattr(runtime, "run_child", lambda *args: pytest.fail("Не запускать worker"))
    with pytest.raises(ValueError):
        execute(context, tmp_path, monkeypatch, environment(tmp_path, **changes))
    assert not (tmp_path / "work").exists()
    assert not (tmp_path / "github-output").exists()


def test_drain_defaults_and_upper_bounds_are_explicit():
    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    options = runtime._drain_options({"DRAIN_BEFORE": BEFORE}, now=now)
    assert (options.max_attempts, options.max_seconds, options.runs_left) == (10, 300, 1)
    options = runtime._drain_options({"DRAIN_BEFORE": BEFORE, "DRAIN_MAX_ATTEMPTS":"30",
        "DRAIN_MAX_SECONDS":"900", "DRAIN_RUNS_LEFT":"200"}, now=now)
    assert (options.max_attempts, options.max_seconds, options.runs_left) == (30, 900, 200)
    with pytest.raises(ValueError):
        runtime._drain_options({"DRAIN_BEFORE": (now+timedelta(seconds=1)).isoformat()}, now=now)


@pytest.mark.parametrize("remaining,left,code,expected", [
    (0, "3", 0, False), (7, "1", 0, False), (7, "3", 1, False), (7, "3", 0, True),
])
def test_chain_requires_success_remaining_work_and_remaining_run_budget(
    context, tmp_path, monkeypatch, remaining, left, code, expected,
):
    def child(command, env):
        write_report(command, remaining=remaining, needs_continue=remaining > 0)
        return code

    monkeypatch.setattr(runtime, "run_child", child)
    assert execute(context, tmp_path, monkeypatch,
                   environment(tmp_path, DRAIN_RUNS_LEFT=left)) == code
    output = (tmp_path / "github-output").read_text()
    assert output == (f"drain_continue={str(expected).lower()}\n"
                      f"drain_next_delay_seconds={60 if expected else 0}\n")


@pytest.mark.parametrize("changes", [
    {"status":"DRAIN_FAILED"}, {"remaining":True}, {"remaining":-1},
    {"needs_continue":"true"}, {"next_delay_seconds":61}, {"next_delay_seconds":"60"},
    {"next_at":"2026-11-19T00:00:00"}, {"next_at":True},
])
def test_invalid_private_report_stops_chain_but_preserves_canonical_checkpoint(
    context, tmp_path, monkeypatch, changes,
):
    def child(command, env):
        write_report(command, **changes)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    assert execute(context, tmp_path, monkeypatch) == 1
    assert context[0].state["status"] == "ready"
    assert context[0].state["last_exit_code"] == 1
    assert (tmp_path / "work/saved/editorial.sqlite3").is_file()
    assert (tmp_path / "github-output").read_text() == (
        "drain_continue=false\ndrain_next_delay_seconds=0\n")


def test_failed_canonical_commit_does_not_authorize_chain(context, tmp_path, monkeypatch):
    def child(command, env):
        write_report(command)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    context[0].fail_commit = True
    with pytest.raises(ValueError, match="conflict"):
        execute(context, tmp_path, monkeypatch)
    assert context[0].state["status"] == "busy"
    assert not (tmp_path / "github-output").exists()


def test_campaign_replay_does_not_book_another_hop(tmp_path):
    command = ["worker", "--output", str(tmp_path / "private")]
    directory = write_report(command, needs_continue=False)
    options = runtime._drain_options(environment(tmp_path), now=datetime.now(UTC))
    assert runtime._drain_control(directory, options, 0) == (False, 0)


@pytest.mark.parametrize("offset,minimum,maximum", [
    (-60, 60, 60), (1800, 1700, 1800), (21600, 21000, 21000), (86400, 21000, 21000),
])
def test_future_next_action_books_one_wait_instead_of_empty_workers(
    tmp_path, offset, minimum, maximum,
):
    directory = write_report(["worker", "--output", str(tmp_path / "private")],
        next_at=(datetime.now(UTC)+timedelta(seconds=offset)).isoformat(), next_delay_seconds=0)
    options = runtime._drain_options(environment(tmp_path), now=datetime.now(UTC))
    should_continue, delay = runtime._drain_control(directory, options, 0)
    assert should_continue and minimum <= delay <= maximum


def test_delivery_flag_follows_existing_actions_setting(context, tmp_path, monkeypatch):
    def child(command, env):
        assert "--send-enabled" not in command
        write_report(command, remaining=0, needs_continue=False)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    assert execute(context, tmp_path, monkeypatch,
                   environment(tmp_path, EDITORIAL_SEND_ENABLED="false")) == 0


def test_owner_close_has_no_send_authority_or_automatic_continuation(
    context, tmp_path, monkeypatch,
):
    monkeypatch.setattr(runtime, "_sweep_material_lifecycle",
                        lambda *args: pytest.fail("Закрытие не запускает общую переоценку"))
    def child(command, env):
        assert "--close-existing" in command and "--send-enabled" not in command
        assert env["EDITORIAL_SEND_ENABLED"] == "false"
        assert not any(key in env for key in (
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "TELEGRAM_BOT_TOKEN", "TYPESAFE_API_KEY"))
        write_report(command, mode="close_existing", closed_by_owner=True,
                     remaining=0, needs_continue=False, next_delay_seconds=0)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    env = environment(tmp_path, DRAIN_RUNS_LEFT="200", GEMINI_API_KEY="PRIVATE_AI",
        GOOGLE_API_KEY="PRIVATE_AI", TELEGRAM_BOT_TOKEN="PRIVATE_BOT",
        TYPESAFE_API_KEY="PRIVATE_JEV")
    assert execute(context, tmp_path, monkeypatch, env, mode="drain-close") == 0
    assert context[0].state["status"] == "ready"
    assert (tmp_path / "github-output").read_text() == (
        "drain_continue=false\ndrain_next_delay_seconds=0\n")


@pytest.mark.parametrize("changes", [
    {"mode": "process"}, {"closed_by_owner": False}, {"closed_by_owner": "true"},
    {"remaining": 1}, {"needs_continue": True},
])
def test_owner_close_requires_confirmed_terminal_report(
    context, tmp_path, monkeypatch, changes,
):
    def child(command, env):
        report = dict(mode="close_existing", closed_by_owner=True, remaining=0,
                      needs_continue=False, next_delay_seconds=0) | changes
        write_report(command, **report)
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    assert execute(context, tmp_path, monkeypatch, mode="drain-close") == 1
    assert context[0].state["status"] == "ready"
    assert (tmp_path / "work/saved/editorial.sqlite3").is_file()
    assert (tmp_path / "github-output").read_text() == (
        "drain_continue=false\ndrain_next_delay_seconds=0\n")


def test_preview_remains_read_only_without_sweeps_or_control(context, tmp_path, monkeypatch):
    before = copy.deepcopy(context[0].state)
    monkeypatch.setattr(runtime, "_sweep_material_lifecycle",
                        lambda *args: pytest.fail("Preview не меняет lifecycle"))
    monkeypatch.setattr(runtime, "run_child", lambda *args: pytest.fail("Preview не worker"))
    assert execute(context, tmp_path, monkeypatch, environment(tmp_path), mode="preview") == 0
    assert context[0].state == before
    assert not (tmp_path / "github-output").exists()


def test_drain_workflow_has_separate_chain_and_same_canonical_lock():
    workflow = yaml.safe_load((ROOT / ".github/workflows/editorial-actions.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    assert "drain" in trigger["mode"]["options"]
    assert "drain-close" in trigger["mode"]["options"]
    assert len(trigger) <= 25
    assert trigger["drain_runs_left"]["default"] == 1
    assert trigger["drain_max_attempts"]["default"] == 10
    assert trigger["drain_max_seconds"]["default"] == 300
    assert "editorial-manual-drain" in workflow["concurrency"]["group"]
    cycle = workflow["jobs"]["cycle"]
    assert cycle["concurrency"]["group"] == "canonical-editorial-state"
    steps = cycle["steps"]
    guard = next(step for step in steps if "exact main CI" in step.get("name", ""))
    assert "git/ref/heads/main" in guard["run"] and "conclusion" in guard["run"]
    chain = workflow["jobs"]["drain_chain"]
    assert chain["needs"] == "cycle" and "concurrency" not in chain
    assert "needs.cycle.result == 'success'" in chain["if"]
    assert "drain_continue == 'true'" in chain["if"]
    assert "inputs.drain_runs_left > 1" in chain["if"]
    body = chain["steps"][0]["run"]
    assert "time.sleep(min(60, remaining))" in body
    assert "60 <= int(delay) <= 21000" in body and chain["timeout-minutes"] == 360
    assert "drain_runs_left={left-1}" in body and "drain_before={before}" in body
    assert "'--ref', 'main'" in body
    assert "secrets." not in json.dumps(chain)
    assert set(cycle["outputs"]) == {
        "projection", "worker_mode", "drain_continue", "drain_next_delay_seconds",
        "morning_watch_date",
    }
    worker_env = next(step['env'] for step in steps if step.get('id') == 'runtime')
    for key in ('GEMINI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'TYPESAFE_API_KEY',
                'EDITORIAL_SEND_ENABLED'):
        assert "!= 'drain-close'" in worker_env[key]
    assert "inputs.mode == 'drain'" in chain['if']


@pytest.mark.parametrize("delay", ["0", "21001", "1\nPRIVATE", "181", "21000"])
def test_chain_script_keeps_exact_cutoff_and_decrements_only_one_hop(monkeypatch, delay):
    workflow = yaml.safe_load((ROOT / ".github/workflows/editorial-actions.yml").read_text())
    body = workflow["jobs"]["drain_chain"]["steps"][0]["run"]
    script = body.split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    clock = [30.0]  # Старт job и подготовка окружения тоже расходуют общий timeout.
    sleeps, commands = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    def dispatch(command, **kwargs):
        commands.append(command)
        clock[0] += 5  # Запрос GitHub тоже занимает время после полного ожидания.

    monkeypatch.setattr(subprocess, "run", dispatch)
    for key, value in {"DRAIN_BEFORE": BEFORE, "DRAIN_MAX_ATTEMPTS":"10",
                       "DRAIN_MAX_SECONDS":"300", "DRAIN_RUNS_LEFT":"200",
                       "DRAIN_DELAY":delay}.items():
        monkeypatch.setenv(key, value)
    if delay not in {"181", "21000"}:
        with pytest.raises(SystemExit):
            exec(compile(script, "drain_chain", "exec"), {})
        assert not commands and not sleeps
    else:
        exec(compile(script, "drain_chain", "exec"), {})
        assert sum(sleeps) == int(delay) and max(sleeps) <= 60
        timeout_seconds = workflow["jobs"]["drain_chain"]["timeout-minutes"] * 60
        assert clock[0] < timeout_seconds
        if delay == "21000":
            assert timeout_seconds - sum(sleeps) == 600
        assert commands == [["gh", "workflow", "run", "editorial-actions.yml", "--ref", "main",
            "-f", "mode=drain", "-f", f"drain_before={BEFORE}",
            "-f", "drain_max_attempts=10", "-f", "drain_max_seconds=300",
            "-f", "drain_runs_left=199"]]


@pytest.mark.parametrize("control", [(True, 21001), (True, True), (True, "21000"),
                                     (True, 0), (False, 60)])
def test_control_output_refuses_out_of_budget_or_non_integer_delay(tmp_path, control):
    with pytest.raises(ValueError):
        runtime._write_drain_control(environment(tmp_path), control)
    assert not (tmp_path / "github-output").exists()
