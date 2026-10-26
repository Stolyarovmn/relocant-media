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


def test_registered_schedules_route_hourly_editorial_and_five_minute_intake(tmp_path):
    schedules = [entry["cron"] for entry in workflow()["on"]["schedule"]]
    assert len(schedules) == 2
    editorial = [cron for cron in schedules
                 if select_mode(tmp_path, event="schedule", cron=cron).splitlines()[-1]
                 == "value=editorial"]
    assert editorial == ["17 * * * *"]
    intake = next(cron for cron in schedules if cron not in editorial)
    assert intake.split()[1:] == ["*", "*", "*", "*"]
    minutes = [int(value) for value in intake.split()[0].split(",")]
    assert minutes == [2, 7, 12, 22, 27, 32, 37, 42, 47, 52, 57]
    assert 17 not in minutes
    assert select_mode(tmp_path, event="schedule", cron=intake).splitlines()[-1] == "value=intake"


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
