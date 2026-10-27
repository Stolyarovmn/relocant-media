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
