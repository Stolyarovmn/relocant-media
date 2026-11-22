"""Execute the real schedule selector without running any editorial worker."""

import configparser
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/editorial-actions.yml")
TIMER = Path("deploy/server/relocant-editorial.timer")


def workflow():
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def select_mode(tmp_path, *, event, cron="", requested=""):
    selector = next(step for step in workflow()["jobs"]["cycle"]["steps"]
                    if step.get("id") == "mode")
    output = tmp_path / "output"
    subprocess.run(["bash", "-eu", "-c", selector["run"]], check=True, env={
        **os.environ, "EVENT_NAME": event, "CRON": cron,
        "REQUESTED_MODE": requested, "GITHUB_OUTPUT": str(output),
    }, capture_output=True, text=True)
    return output.read_text().strip()


def test_redundant_schedules_route_editorial_and_intake_without_slot_overlap(tmp_path):
    """GitHub drops best-effort cron slots, so each worker has independent backup slots."""
    schedules = [entry["cron"] for entry in workflow()["on"]["schedule"]]
    routed = {cron: select_mode(tmp_path, event="schedule", cron=cron).splitlines()[-1]
              for cron in schedules}
    editorial = [cron for cron, value in routed.items() if value == "value=editorial"]
    intake = [cron for cron, value in routed.items() if value == "value=intake"]
    assert editorial == ["17 * * * *", "47 * * * *"]
    assert len(intake) == 2
    assert {cron for cron, mode in routed.items() if mode == "value=digest"} == {
        "30 5,6 * * *", "30 18,19 * * *",
    }
    editorial_minutes = {17, 47}
    intake_minutes = []
    for cron in intake:
        fields = cron.split()
        assert fields[1:] == ["*", "*", "*", "*"]
        intake_minutes += [int(value) for value in fields[0].split(",")]
    assert len(intake_minutes) == len(set(intake_minutes))
    assert not editorial_minutes & set(intake_minutes)
    assert {2, 7, 12, 22, 27, 32, 37, 42, 52, 57} <= set(intake_minutes)


@pytest.mark.parametrize("mode", ["preview", "editorial", "intake", "discovery",
                                  "fallback-preview", "fallback"])
def test_manual_mode_is_preserved_instead_of_treating_dispatch_as_intake(tmp_path, mode):
    assert select_mode(tmp_path, event="workflow_dispatch", requested=mode) == f"value={mode}"


def test_hourly_timer_retains_single_service_and_persistent_serialized_runtime():
    timer = configparser.ConfigParser()
    timer.read(TIMER)
    assert timer["Timer"]["OnCalendar"] == "*-*-* *:00:00 UTC"
    assert timer["Timer"]["Unit"] == "relocant-editorial.service"
    assert timer["Timer"]["Persistent"] == "true"
    data = workflow()
    assert data["jobs"]["cycle"]["concurrency"] == {
        "group": "canonical-editorial-state", "cancel-in-progress": "false",
        "queue": "max",
    }
    assert "vars.ACTIONS_EDITORIAL_ENABLED == 'true'" in data["jobs"]["cycle"]["if"]
    runtime = next(step for step in data["jobs"]["cycle"]["steps"]
                   if step.get("id") == "runtime")
    assert runtime["env"]["EDITORIAL_SEND_ENABLED"] == (
        "${{ steps.mode.outputs.value != 'drain-close' "
        "&& vars.ACTIONS_SEND_ENABLED || 'false' }}"
    )


def _chain():
    return workflow()["jobs"]["chain"]


def retry_workflow():
    path = WORKFLOW.with_name("editorial-retry.yml")
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def chain_enabled(*, event, mode="", result="success", enabled=True, branch="main",
                  start_status="ready"):
    """Evaluate the actual job gate with bounded literal contexts, without a runner."""
    gate = _chain()["if"]
    replacements = {
        "!startsWith(inputs.mode, 'map-')": repr(not mode.startswith("map-")),
        "always()": "True",
        "github.ref": repr(f"refs/heads/{branch}"),
        "github.event_name": repr(event),
        "needs.cycle.result": repr(result),
        "needs.cycle.outputs.start_status": repr(start_status),
        "needs.cycle.outputs.worker_mode": repr(mode),
        "vars.ACTIONS_EDITORIAL_ENABLED": repr("true" if enabled else "false"),
        "vars.ACTIONS_CHAIN_ENABLED": repr("true" if enabled else "false"),
        "inputs.mode": repr(mode),
    }
    for source, value in replacements.items():
        gate = gate.replace(source, value)
    gate = gate.replace("&&", " and ").replace("||", " or ")
    return eval(gate, {"__builtins__": {}}, {})


@pytest.mark.parametrize("result", ["success", "failure"])
@pytest.mark.parametrize("mode", ["intake", "editorial", "discovery", "preview", "recovery",
                                  "digest", "digest-preview", "fallback", "map-backfill"])
def test_manual_operations_never_hold_their_queue_for_chain_sleep(mode, result):
    assert chain_enabled(event="workflow_dispatch", mode=mode, result=result) is False


@pytest.mark.parametrize("result", ["success", "failure"])
@pytest.mark.parametrize("event,mode", [("schedule", ""), ("workflow_dispatch", "auto")])
def test_automatic_runs_keep_success_and_failure_continuation(event, mode, result):
    assert chain_enabled(event=event, mode=mode, result=result) is True


@pytest.mark.parametrize("context", [
    {"result": "cancelled"}, {"result": "skipped"}, {"enabled": False}, {"branch": "feature"},
    {"start_status": "superseded"}, {"start_status": "ci_pending"}, {"start_status": "ci_failed"},
])
def test_automatic_chain_still_respects_stop_conditions(context):
    assert chain_enabled(event="workflow_dispatch", mode="auto", **context) is False


def test_chain_survives_a_failed_cycle_but_backs_off():
    """Operational failures after start still use the established backoff."""
    chain = _chain()
    # Without a status function GitHub adds an implicit success(), which silently skips this
    # job after a failed cycle (this ended the chain at 04:20 on 28.10.2026).
    assert "always()" in chain["if"]
    assert "needs.cycle.result == 'failure'" in chain["if"]
    assert "needs.cycle.result == 'cancelled'" not in chain["if"]
    delay = retry_workflow()["jobs"]["retry"]["steps"][-2]["env"]["CHAIN_DELAY_SECONDS"]
    assert delay == (
        "${{ inputs.result == 'success' && '120' || steps.failure-delay.outputs.seconds || '600' }}"
    )


def test_chain_books_the_next_cycle_only_after_a_finished_main_cycle():
    chain = _chain()
    assert chain["needs"] == "cycle"
    for condition in ("needs.cycle.result == 'success'", "refs/heads/main",
                      "vars.ACTIONS_EDITORIAL_ENABLED == 'true'",
                      "vars.ACTIONS_CHAIN_ENABLED == 'true'", "inputs.mode != 'preview'"):
        assert condition in chain["if"]
    assert chain["permissions"] == {"actions": "write"}
    assert "auto" in workflow()["on"]["workflow_dispatch"]["inputs"]["mode"]["options"]


def test_manual_operations_have_separate_workflow_queues_and_shared_state_lock():
    data = workflow()
    assert data["concurrency"]["cancel-in-progress"] == "false"
    assert data["jobs"]["cycle"]["concurrency"]["group"] == "canonical-editorial-state"
    assert "inputs.mode != 'fallback-preview'" in _chain()["if"]
    assert "inputs.mode != 'fallback'" in _chain()["if"]
    assert "!startsWith(inputs.mode, 'map-')" in _chain()["if"]
    assert "inputs.mode != 'digest-preview'" in _chain()["if"]
    assert "inputs.mode != 'digest'" in _chain()["if"]
    assert "needs.cycle.outputs.worker_mode != 'digest'" in _chain()["if"]


def test_preflight_retry_requires_an_unstarted_cycle_and_releases_the_writer():
    data = workflow()
    retry = data["jobs"]["start_retry"]
    assert retry["needs"] == "cycle"
    assert retry["concurrency"]["group"] != data["jobs"]["cycle"]["concurrency"]["group"]
    assert retry["concurrency"]["cancel-in-progress"] == "false"
    assert retry["permissions"] == {"actions": "write", "contents": "read"}
    assert "github.run_attempt == '1' && !inputs.preflight_retry" in retry["if"]
    assert "needs.cycle.result == 'success'" in retry["if"]
    assert "needs.cycle.outputs.start_status == 'superseded'" in retry["if"]
    assert "needs.cycle.outputs.start_status == 'ci_pending'" in retry["if"]
    assert "vars.ACTIONS_EDITORIAL_ENABLED == 'true'" in retry["if"]
    assert "vars.ACTIONS_CUTOVER_CONFIRMED == 'true'" in retry["if"]
    assert data["on"]["workflow_dispatch"]["inputs"]["preflight_retry"]["default"] == "false"
    assert retry["steps"][0]["with"]["persist-credentials"] == "false"
    assert "secrets." not in str(retry)


@pytest.mark.parametrize(('mode', 'schedule', 'expected'), [
    ('auto', '', 'canonical-editorial-writer'),
    ('', '17 * * * *', 'canonical-editorial-writer'),
    ('intake', '', 'editorial-manual-intake'),
    ('editorial', '', 'editorial-manual-cycle'),
    ('discovery', '', 'editorial-manual-discovery'),
    ('drain', '', 'editorial-manual-drain'),
    ('drain-close', '', 'editorial-close-backlog'),
    ('preview', '', 'editorial-preview'),
    ('fallback-preview', '', 'editorial-subscription-gpt'),
    ('fallback', '', 'editorial-subscription-gpt'),
    ('map-backfill', '', 'editorial-map-operations'),
    ('digest-preview', '', 'editorial-digest-preview'),
    ('digest', '', 'editorial-digest'),
    ('', '30 5,6 * * *', 'editorial-digest'),
    ('', '30 18,19 * * *', 'editorial-digest'),
])
def test_workflow_queue_isolated_from_the_automatic_chain(mode, schedule, expected):
    expression = workflow()['concurrency']['group'].removeprefix('${{').removesuffix('}}')
    event = 'schedule' if schedule else 'workflow_dispatch'
    for prefix in ('fallback', 'map-'):
        expression = expression.replace(
            f"startsWith(inputs.mode, '{prefix}')", repr(mode.startswith(prefix)),
        )
    expression = expression.replace('inputs.mode', repr(mode))
    expression = expression.replace('github.event_name', repr(event))
    expression = expression.replace('github.event.schedule', repr(schedule))
    expression = ' '.join(expression.replace('&&', ' and ').replace('||', ' or ').split())
    assert eval(expression, {'__builtins__': {}}, {}) == expected


def test_published_correction_is_manual_main_only_and_shares_state_lock():
    path = WORKFLOW.with_name("editorial-correction.yml")
    correction = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(correction["on"]) == {"workflow_dispatch"}
    assert set(correction["on"]["workflow_dispatch"]["inputs"]) == {"plan_sha256"}
    assert correction["permissions"] == {"contents": "read", "actions": "read"}
    job = correction["jobs"]["correct"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert job["concurrency"] == workflow()["jobs"]["cycle"]["concurrency"]
    steps = job["steps"]
    gate = next(i for i, step in enumerate(steps) if "exact main CI" in step.get("name", ""))
    edit = next(i for i, step in enumerate(steps)
                if "ops.correction_delivery" in step.get("run", ""))
    assert gate < edit and "TELEGRAM_BOT_TOKEN" not in steps[gate]["env"]
    assert not any("uses" in step and "upload-" in step["uses"] for step in steps)


@pytest.mark.parametrize("result", ["success", "failure"])
def test_chain_books_the_retry_without_waiting_in_the_writer_queue(monkeypatch, result):
    step = _chain()["steps"][0]
    assert step["shell"] == "python" and "sleep" not in step["run"]
    monkeypatch.setenv("GH_REPO", "Stolyarovmn/relocant-media")
    monkeypatch.setenv("CYCLE_RESULT", result)
    monkeypatch.setenv("CYCLE_SHA", "a" * 40)
    monkeypatch.setenv("CYCLE_RUN_ID", "123")
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda args, *, check: commands.append(args))
    exec(step["run"], {})
    assert commands == [[
        "gh", "workflow", "run", "editorial-retry.yml", "-R", "Stolyarovmn/relocant-media",
        "--ref", "main", "-f", f"result={result}", "-f", f"source_sha={'a' * 40}",
        "-f", "source_run_id=123",
    ]]


def test_retry_has_its_own_queue_and_cancels_only_previous_waits():
    data = retry_workflow()
    assert set(data["on"]) == {"workflow_dispatch"}
    assert data["permissions"] == {}
    job = data["jobs"]["retry"]
    assert job["concurrency"] == {"group": "editorial-retry-clock", "cancel-in-progress": "true"}
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    assert "inputs.source_sha == github.sha" in job["if"]
    assert "refs/heads/main" in job["if"]
    assert "vars.ACTIONS_CHAIN_ENABLED == 'true'" in job["if"]
    checkout, detect, *_ = job["steps"]
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["if"] == detect["if"]
    assert detect["if"] == "inputs.result == 'failure' && inputs.source_run_id != ''"
    assert checkout["continue-on-error"] == detect["continue-on-error"] == "true"


@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.parametrize("delay", [120, 600, 1200, 1800])
def test_retry_waits_and_resumes_only_the_current_version(monkeypatch, delay, advanced):
    wait, launch = retry_workflow()["jobs"]["retry"]["steps"][-2:]
    monkeypatch.setenv("CHAIN_DELAY_SECONDS", str(delay))
    monkeypatch.setenv("GH_REPO", "Stolyarovmn/relocant-media")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    timeline = []
    monkeypatch.setattr(time, "sleep", lambda seconds: timeline.append(("sleep", seconds)))

    def read_head(args, *, text):
        timeline.append(("read", args))
        return ("b" if advanced else "a") * 40 + "\n"

    monkeypatch.setattr(subprocess, "check_output", read_head)
    monkeypatch.setattr(subprocess, "run", lambda args, *, check: timeline.append(("run", args)))
    exec(wait["run"], {})
    exec(launch["run"], {})
    assert timeline[:2] == [("sleep", delay), ("read", [
        "gh", "api", "repos/Stolyarovmn/relocant-media/git/ref/heads/main", "--jq", ".object.sha",
    ])]
    assert timeline[2:] == ([] if advanced else [("run", [
        "gh", "workflow", "run", "editorial-actions.yml", "-R", "Stolyarovmn/relocant-media",
        "--ref", "main", "-f", "mode=auto",
    ])])
