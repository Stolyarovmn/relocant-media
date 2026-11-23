"""Fail closed before storage and verify bounded replacement against the real response shape."""

from unittest.mock import Mock

import pytest

from relocant_media.ops import editorial_start as start

REPO = "Stolyarovmn/relocant-media"
OLD = "a" * 40
NEW = "b" * 40
GPT_RESULT = "c" * 64


@pytest.mark.parametrize(("status", "conclusion", "expected"), [
    ("queued", None, "ci_pending"),
    ("in_progress", None, "ci_pending"),
    ("completed", "failure", "ci_failed"),
    ("completed", "cancelled", "ci_failed"),
    ("completed", "success", "ready"),
])
def test_checks_latest_push_ci_for_exact_main(monkeypatch, status, conclusion, expected):
    run = {
        "head_sha": NEW, "head_branch": "main", "event": "push",
        "path": ".github/workflows/ci.yml", "status": status, "conclusion": conclusion,
    }
    api = Mock(return_value={"workflow_runs": [run]})
    monkeypatch.setattr(start, "api", api)
    assert start.ci_status(REPO, NEW) == expected
    api.assert_called_once_with(REPO, "actions/workflows/ci.yml/runs"
                                f"?branch=main&event=push&head_sha={NEW}&per_page=1")
    run["head_sha"] = OLD
    with pytest.raises(ValueError, match="exact main push"):
        start.ci_status(REPO, NEW)
    api.return_value = {"workflow_runs": []}
    assert start.ci_status(REPO, NEW) == "ci_pending"


@pytest.mark.parametrize("heads", [[NEW], [OLD, NEW]])
def test_stale_sha_never_enters_the_writer(monkeypatch, heads):
    monkeypatch.setattr(start, "main_sha", Mock(side_effect=heads))
    ci = Mock(return_value="ready")
    monkeypatch.setattr(start, "ci_status", ci)
    assert start.start_status(REPO, OLD) == "superseded"
    assert ci.call_count == len(heads) - 1


@pytest.mark.parametrize(("mode", "retry", "status", "returncode"), [
    ("editorial", "false", "superseded", 0),
    ("digest", "false", "ci_pending", 0),
    ("auto", "true", "superseded", 1),
    ("fallback", "false", "superseded", 1),
    ("map-republish", "false", "ci_pending", 1),
    ("editorial", "false", "ci_failed", 1),
    ("intake", "true", "ready", 0),
])
def test_gate_marks_exact_state_and_only_replaces_plain_first_attempts(
    monkeypatch, tmp_path, mode, retry, status, returncode,
):
    output = tmp_path / "output"
    monkeypatch.setattr(start.sys, "argv", ["editorial_start.py", "gate"])
    monkeypatch.setattr(start, "start_status", Mock(return_value=status))
    for key, value in {
        "GITHUB_REPOSITORY": REPO, "WORKER_MODE": mode, "PREFLIGHT_RETRY": retry,
        "GITHUB_SHA": OLD, "GITHUB_OUTPUT": str(output), "GITHUB_RUN_ATTEMPT": "1",
        "PREFLIGHT_RETRY_ENABLED": "true", "GPT_FALLBACK_BLOB_SHA256": "",
        "GPT_FALLBACK_CYCLE_ID": "",
    }.items():
        monkeypatch.setenv(key, value)
    assert start.main() == returncode
    assert output.read_text() == f"status={status}\n"


@pytest.mark.parametrize(("flag", "value"), [
    ("PREFLIGHT_RETRY_ENABLED", "false"), ("GITHUB_RUN_ATTEMPT", "2"),
])
def test_stopped_gate_fails_when_replacement_is_disabled(monkeypatch, tmp_path, flag, value):
    monkeypatch.setattr(start.sys, "argv", ["editorial_start.py", "gate"])
    monkeypatch.setattr(start, "start_status", Mock(return_value="superseded"))
    for key, state in {
        "GITHUB_REPOSITORY": REPO, "WORKER_MODE": "editorial", "GITHUB_SHA": OLD,
        "GITHUB_OUTPUT": str(tmp_path / "output"), "PREFLIGHT_RETRY_ENABLED": "true",
        "GITHUB_RUN_ATTEMPT": "1", flag: value,
    }.items():
        monkeypatch.setenv(key, state)
    assert start.main() == 1


def test_deduplicates_only_same_mode_on_the_current_main(monkeypatch):
    other = {
        "id": 123, "status": "queued", "head_branch": "main", "head_sha": NEW,
        "display_title": start.RETRY_TITLES["editorial"], "event": "workflow_dispatch",
    }
    api = Mock(return_value={"workflow_runs": [other]})
    monkeypatch.setattr(start, "api", api)
    for key, value in {
        "id": 456, "head_sha": OLD, "display_title": start.RETRY_TITLES["auto"],
        "status": "completed", "event": "schedule",
    }.items():
        api.return_value = {"workflow_runs": [{**other, key: value}]}
        assert not start.already_scheduled(REPO, sha=NEW, mode="editorial", source_run="456")
    api.return_value = {"workflow_runs": [other]}
    assert start.already_scheduled(REPO, sha=NEW, mode="editorial", source_run="456")


@pytest.mark.parametrize(("outcomes", "queued", "decision"), [
    (["ci_pending", "ready"], False, "dispatched"),
    (["ready"], True, "already_scheduled"),
    (["ci_failed"], False, "ci_failed"),
])
def test_waits_on_current_ci_then_dispatches_exact_mode_at_most_once(
    monkeypatch, outcomes, queued, decision,
):
    calls = Mock(side_effect=outcomes)
    monkeypatch.setattr(start, "ci_status", calls)
    monkeypatch.setattr(start, "main_sha", Mock(return_value=NEW))
    coalesce = Mock(return_value=queued)
    monkeypatch.setattr(start, "already_scheduled", coalesce)
    pause = Mock()
    monkeypatch.setattr(start.time, "sleep", pause)
    dispatch = Mock()
    monkeypatch.setattr(start.subprocess, "run", dispatch)
    assert start.restart(REPO, mode="editorial", source_run="456") == decision
    assert pause.call_count == len(outcomes) - 1
    if decision == "dispatched":
        dispatch.assert_called_once_with([
            "gh", "workflow", "run", "editorial-actions.yml", "-R", REPO,
            "--ref", "main", "-f", "mode=editorial", "-f", "preflight_retry=true",
        ], check=True, timeout=20)
    else:
        dispatch.assert_not_called()


def test_wait_and_restart_follow_a_new_main_after_verification(monkeypatch):
    monkeypatch.setattr(start, "main_sha", Mock(side_effect=[OLD, NEW, NEW, NEW]))
    check = Mock(return_value="ready")
    monkeypatch.setattr(start, "ci_status", check)
    monkeypatch.setattr(start, "already_scheduled", Mock(return_value=False))
    monkeypatch.setattr(start.time, "sleep", Mock())
    dispatch = Mock()
    monkeypatch.setattr(start.subprocess, "run", dispatch)
    assert start.restart(REPO, mode="intake", source_run="456") == "dispatched"
    assert [call.args for call in check.call_args_list] == [(REPO, OLD), (REPO, NEW)]
    assert dispatch.call_count == 1


def test_pending_ci_exhausts_a_single_budget(monkeypatch):
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(start.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(start.time, "sleep", sleep)
    monkeypatch.setattr(start, "main_sha", Mock(return_value=NEW))
    monkeypatch.setattr(start, "ci_status", Mock(return_value="ci_pending"))
    dispatch = Mock()
    monkeypatch.setattr(start.subprocess, "run", dispatch)
    assert start.restart(REPO, mode="auto", source_run="456") == "ci_pending"
    assert clock[0] == 360
    dispatch.assert_not_called()


@pytest.mark.parametrize(("mode", "blob", "cycle", "expected"), [
    ("fallback", GPT_RESULT, "", ["-f", f"fallback_blob_sha256={GPT_RESULT}"]),
    ("fallback-preview", "", "12313", ["-f", "fallback_cycle_id=12313"]),
    ("fallback-preview", "", "", []),
])
def test_gpt_retry_preserves_original_scope_without_coalescing_by_title(
    monkeypatch, mode, blob, cycle, expected,
):
    monkeypatch.setattr(start, "main_sha", Mock(return_value=NEW))
    monkeypatch.setattr(start, "ci_status", Mock(return_value="ready"))
    coalesce = Mock(return_value=True)
    monkeypatch.setattr(start, "already_scheduled", coalesce)
    dispatch = Mock()
    monkeypatch.setattr(start.subprocess, "run", dispatch)
    assert start.restart(
        REPO, mode=mode, source_run="456", blob=blob, cycle_id=cycle,
    ) == "dispatched"
    coalesce.assert_not_called()
    dispatch.assert_called_once_with([
        "gh", "workflow", "run", "editorial-actions.yml", "-R", REPO,
        "--ref", "main", "-f", f"mode={mode}", "-f", "preflight_retry=true", *expected,
    ], check=True, timeout=20)


@pytest.mark.parametrize(("mode", "blob", "cycle", "attempt", "is_retry", "expected"), [
    ("fallback", GPT_RESULT, "", "1", "false", 0),
    ("fallback", GPT_RESULT, "", "2", "false", 1),
    ("fallback", GPT_RESULT, "", "1", "true", 1),
    ("fallback", "no-digest", "", "1", "false", 1),
    ("fallback-preview", "", "12313", "1", "false", 0),
    ("fallback-preview", "", "bad-id", "1", "false", 1),
])
def test_gpt_gate_allows_single_replacement_only_with_valid_inputs(
    monkeypatch, tmp_path, mode, blob, cycle, attempt, is_retry, expected,
):
    output = tmp_path / "output"
    monkeypatch.setattr(start.sys, "argv", ["editorial_start.py", "gate"])
    monkeypatch.setattr(start, "start_status", Mock(return_value="superseded"))
    for key, value in {
        "GITHUB_REPOSITORY": REPO, "WORKER_MODE": mode, "GITHUB_SHA": OLD,
        "GITHUB_OUTPUT": str(output), "GITHUB_RUN_ATTEMPT": attempt,
        "PREFLIGHT_RETRY_ENABLED": "true", "PREFLIGHT_RETRY": is_retry,
        "GPT_FALLBACK_BLOB_SHA256": blob, "GPT_FALLBACK_CYCLE_ID": cycle,
    }.items():
        monkeypatch.setenv(key, value)
    assert start.main() == expected
    assert output.read_text() == "status=superseded\n"
