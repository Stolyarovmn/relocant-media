"""Execute the real schedule selector without running any editorial worker."""

import configparser
import os
import subprocess
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
    assert len(intake) == 2 and len(schedules) == 4
    editorial_minutes = {17, 47}
    intake_minutes = []
    for cron in intake:
        fields = cron.split()
        assert fields[1:] == ["*", "*", "*", "*"]
        intake_minutes += [int(value) for value in fields[0].split(",")]
    assert len(intake_minutes) == len(set(intake_minutes))
    assert not editorial_minutes & set(intake_minutes)
    assert {2, 7, 12, 22, 27, 32, 37, 42, 52, 57} <= set(intake_minutes)


@pytest.mark.parametrize("mode", ["preview", "editorial", "intake", "discovery"])
def test_manual_mode_is_preserved_instead_of_treating_dispatch_as_intake(tmp_path, mode):
    assert select_mode(tmp_path, event="workflow_dispatch", requested=mode) == f"value={mode}"


def test_hourly_timer_retains_single_service_and_persistent_serialized_runtime():
    timer = configparser.ConfigParser()
    timer.read(TIMER)
    assert timer["Timer"]["OnCalendar"] == "*-*-* *:00:00 UTC"
    assert timer["Timer"]["Unit"] == "relocant-editorial.service"
    assert timer["Timer"]["Persistent"] == "true"
    data = workflow()
    assert data["concurrency"] == {
        "group": "canonical-editorial-writer", "cancel-in-progress": "false",
    }
    assert "vars.ACTIONS_EDITORIAL_ENABLED == 'true'" in data["jobs"]["cycle"]["if"]
    runtime = next(step for step in data["jobs"]["cycle"]["steps"]
                   if step.get("id") == "runtime")
    assert runtime["env"]["EDITORIAL_SEND_ENABLED"] == (
        "${{ vars.ACTIONS_SEND_ENABLED || 'false' }}"
    )


def _chain():
    return workflow()["jobs"]["chain"]


def test_chain_survives_a_failed_cycle_but_backs_off():
    """The exact-main-CI guard fails right after every merge; that must pause the chain
    for ten minutes, not end it. Cancelled or skipped cycles still do not chain."""
    chain = _chain()
    # Without a status function GitHub adds an implicit success(), which silently skips this
    # job after a failed cycle (this ended the chain at 04:20 on 28.10.2026).
    assert "always()" in chain["if"]
    assert "needs.cycle.result == 'failure'" in chain["if"]
    assert "needs.cycle.result == 'cancelled'" not in chain["if"]
    delay = chain["steps"][0]["env"]["CHAIN_DELAY_SECONDS"]
    assert "needs.cycle.result == 'success'" in delay and "'120'" in delay and "'600'" in delay


def test_chain_books_the_next_cycle_only_after_a_finished_main_cycle():
    chain = _chain()
    assert chain["needs"] == "cycle"
    for condition in ("needs.cycle.result == 'success'", "refs/heads/main",
                      "vars.ACTIONS_EDITORIAL_ENABLED == 'true'",
                      "vars.ACTIONS_CHAIN_ENABLED == 'true'", "inputs.mode != 'preview'"):
        assert condition in chain["if"]
    assert chain["permissions"] == {"actions": "write"}
    assert "auto" in workflow()["on"]["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert workflow()["concurrency"]["group"] == "canonical-editorial-writer"


def _run_chain(tmp_path, queued):
    script = next(step for step in _chain()["steps"])["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls"
    stub = ["#!/bin/sh", 'echo "$@" >> "$CALLS"', '[ "$1" = run ] && echo "$QUEUED"', "exit 0", ""]
    (bin_dir / "gh").write_text(chr(10).join(stub), encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    subprocess.run(["bash", "-eu", "-c", script], check=True, env={
        **os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(calls),
        "QUEUED": str(queued), "CHAIN_DELAY_SECONDS": "0",
    })
    return calls.read_text().splitlines()


def test_chain_books_the_next_cycle_even_when_a_cron_run_is_already_waiting(tmp_path):
    """The concurrency group keeps one pending run, so booking again can never pile up;
    a waiting cron run is a normal moment of every cycle and must not break the chain."""
    for queued in (0, 1, 3):
        calls = _run_chain(tmp_path / f"queued{queued}", queued)
        assert calls[-1] == "workflow run editorial-actions.yml --ref main -f mode=auto"
