import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/editorial-after-ci.yml"
SHA = "a" * 40
REPO = "Stolyarovmn/relocant-media"


@pytest.fixture
def workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_resume_only_trusts_successful_push_ci_from_current_main(workflow):
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {"workflow_run": {
        "workflows": ["ci"], "types": ["completed"], "branches": ["main"],
    }}
    job = workflow["jobs"]["resume"]
    assert all(clause in job["if"] for clause in (
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.head_sha == github.sha",
        "vars.ACTIONS_EDITORIAL_ENABLED == 'true'",
        "vars.ACTIONS_CUTOVER_CONFIRMED == 'true'",
    ))
    assert workflow["permissions"] == {}
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    assert len(job["steps"]) == 1 and "uses" not in job["steps"][0]


@pytest.mark.parametrize(("path", "head", "outcome"), (
    (".github/workflows/ci.yml", SHA, "dispatched"),
    (".github/workflows/ci.yml", "b" * 40, "main_advanced"),
    (".github/workflows/other.yml", SHA, "wrong_ci_workflow"),
))
def test_resume_checks_path_and_latest_main_before_dispatch(
    workflow, path, head, outcome, monkeypatch, capsys,
):
    monkeypatch.setenv("CI_RUN_ID", "123")
    monkeypatch.setenv("CI_SHA", SHA)
    responses = {
        ("gh", "api", f"repos/{REPO}/actions/runs/123", "--jq", ".path"): path,
        ("gh", "api", f"repos/{REPO}/git/ref/heads/main", "--jq", ".object.sha"): head,
    }
    monkeypatch.setattr(subprocess, "check_output", lambda args, **kwargs: responses[tuple(args)])
    dispatch = Mock()
    monkeypatch.setattr(subprocess, "run", dispatch)
    program = workflow["jobs"]["resume"]["steps"][0]["run"]
    exec(compile(program, str(WORKFLOW), "exec"), {})
    assert f"editorial_resume={outcome}" in capsys.readouterr().out
    if outcome == "dispatched":
        dispatch.assert_called_once_with(
            ["gh", "workflow", "run", "editorial-actions.yml", "-R", REPO,
             "--ref", "main", "-f", "mode=auto"],
            check=True,
        )
    else:
        dispatch.assert_not_called()
