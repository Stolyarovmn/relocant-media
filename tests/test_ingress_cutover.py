import copy
import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from relocant_media.ai.package import canonical_json
from relocant_media.ops import ingress_cutover as cutover
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID

TOKEN = "123456:private_bot_token_for_fake_calls_only"
BRIDGE = "fake_bridge_secret_" + "b" * 32
SECRET = "fake_webhook_secret_" + "s" * 32
AT = datetime(2026, 11, 18, 12, tzinfo=UTC)


class Storage:
    def __init__(self, trace):
        self.state = {
            "version": 1, "owner": "github-actions", "status": "ready",
            "generation": "initial", "cutover": {"server_fenced": True},
            "snapshot": {"sha256": "a" * 64, "drive_file_id": "snapshot-id"},
            "intake_state": {"attempts": 1},
        }
        self.sha = "first"
        self.files = {}
        self.trace = trace
        self.fail_phase = None
        self.fail_claim = False

    def read(self):
        return copy.deepcopy(self.state), self.sha

    def write(self, state, previous):
        assert previous == self.sha
        assert state["cutover"] == self.state["cutover"]
        if self.state["status"] == "ready":
            assert state["status"] == "busy"
            assert state["snapshot"] == self.state["snapshot"]
            assert state["generation"] == self.state["generation"]
            assert isinstance(state.get("attempt"), str) and state["attempt"]
            if self.fail_claim:
                raise ValueError("conditional claim failed")
            phase = self.state["intake_state"].get("ingress", {}).get("phase", "unprepared")
            label = f"busy:{phase}"
        else:
            assert self.state["status"] == "busy" and state["status"] == "ready"
            assert state.get("attempt") == self.state["attempt"]
            assert state["generation"] == self.state["attempt"]
            phase = state["intake_state"].get("ingress", {}).get("phase", "polling")
            label = phase
        if state["status"] == "ready" and self.fail_phase == phase:
            raise ValueError("conditional write failed")
        self.trace.append(label)
        self.state = copy.deepcopy(state)
        self.sha += "x"
        return self.sha

    def upload(self, path, folder):
        assert folder in {"intake-seed", "ingress-rollback-audit"}
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.files[digest] = data
        self.trace.append("upload" if folder == "intake-seed" else
                          f"audit:{json.loads(data)['phase']}")
        return digest

    def download(self, digest):
        self.trace.append("download")
        return self.files[digest]


@pytest.fixture
def scenario(monkeypatch):
    trace = []
    storage = Storage(trace)
    seed = {"version": 1, "created_at": AT.isoformat(), "bot_id": 123456,
            "channel_id": APPROVED_CHANNEL_ID, "form": {}, "cases": [],
            "processed_updates": [100], "legacy_conversations": []}
    exports = []

    def export(*args):
        exports.append(args)
        return copy.deepcopy(seed)

    monkeypatch.setattr(cutover, "_export", export)
    config = {"url": "", "max_connections": 1}
    requests = []
    fail = {}

    def handle(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        requests.append((operation, payload))
        trace.append(operation)
        if operation == fail.get("operation"):
            return httpx.Response(500, json={"ok": False})
        if operation == "getMe":
            result = {"id": 123456, "is_bot": True, "username": cutover.BOT_USERNAME}
            result.update(fail.get("identity", {}))
        elif operation == "getChatMember":
            result = {"can_manage_direct_messages": True}
        elif operation == "getWebhookInfo":
            result = dict(config)
        elif operation == "setWebhook":
            assert storage.state["status"] == "ready"
            assert storage.state["intake_state"]["ingress"]["phase"] in {"prepared", "active"}
            assert payload == {"url": cutover.PRODUCTION_ORIGIN + "/api/telegram",
                               "secret_token": SECRET, "max_connections": 1,
                               "drop_pending_updates": False}
            config.update(url=payload["url"])
            config.update(fail.get("verified_webhook", {}))
            result = True
        elif operation == "transfer":
            assert storage.state["status"] == "ready"
            assert request.headers["Authorization"] == f"Bearer {BRIDGE}"
            assert storage.state["intake_state"]["ingress"]["phase"] in {"prepared", "active"}
            if payload["operation"] == "metadata":
                if fail.get("metadata_error"):
                    return httpx.Response(503)
                metadata = {
                    "status": "ready",
                    "state": {"etag_sha256": "d" * 64, "uploaded_at": AT.isoformat(),
                              "size": 7060},
                    "config": {"etag_sha256": "e" * 64, "uploaded_at": AT.isoformat(),
                               "size": 259},
                }
                updates = fail.get("metadata_responses", [])
                if updates:
                    for key, changes in updates.pop(0).items():
                        metadata[key].update(changes)
                return httpx.Response(200, json=metadata)
            assert payload["operation"] == "bootstrap"
            assert payload["seed_sha256"] in storage.files
            result = {"status": "ready", "can_send": True,
                      "seed_sha256": payload["seed_sha256"]}
            result.update(fail.get("receiver", {}))
            return httpx.Response(200, json=result)
        elif operation == "deleteWebhook":
            assert payload == {"drop_pending_updates": False}
            assert storage.state["status"] == "ready"
            assert storage.state["intake_state"]["ingress"]["phase"] == "prepared"
            assert storage.state["ingress_rollback"]["phase"] == "prepared"
            config["url"] = ""
            config.update(fail.get("verified_webhook", {}))
            result = True
        else:
            pytest.fail("Неизвестная операция фальшивого API")
        return httpx.Response(200, json={"ok": True, "result": result})

    with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as client:
        yield storage, client, trace, exports, config, requests, fail, seed


def run(scenario, tmp_path, *, activate=False, name="work", now=AT, url=None):
    storage, client, *_ = scenario
    return cutover.execute(storage, url or cutover.PRODUCTION_ORIGIN, tmp_path / name,
                           client, TOKEN, BRIDGE, SECRET, activate=activate, now=now)


def test_preflight_only_reads_and_returns_private_free_counts(scenario, tmp_path):
    storage, _, trace, exports, _, _, _, _ = scenario
    before = storage.read()
    result = run(scenario, tmp_path)
    assert result["status"] == "preflight_ready"
    assert storage.read() == before and storage.files == {}
    assert trace == ["getMe", "getChatMember", "getWebhookInfo"]
    assert len(exports) == 1
    assert all(secret not in json.dumps(result) for secret in (TOKEN, BRIDGE, SECRET))


def test_activation_fences_before_bootstrap_and_verifies_before_active(scenario, tmp_path):
    storage, _, trace, _, _, requests, _, _ = scenario
    result = run(scenario, tmp_path, activate=True)
    assert result["status"] == "active"
    assert trace == ["getMe", "getChatMember", "getWebhookInfo", "upload", "busy:unprepared",
                     "prepared", "transfer", "setWebhook", "getWebhookInfo",
                     "busy:prepared", "active"]
    assert storage.state["intake_state"]["attempts"] == 1
    marker = storage.state["intake_state"]["ingress"]
    assert marker["seed_snapshot_sha256"] == "a" * 64
    assert storage.files[marker["seed_sha256"]] == canonical_json(scenario[-1]).encode()
    assert all("allowed_updates" not in payload for _, payload in requests)


@pytest.mark.parametrize("failure", ["transfer", "setWebhook"])
def test_failure_leaves_prepared_fence_and_retry_reuses_exact_seed(scenario, tmp_path, failure):
    storage, _, trace, exports, config, _, fail, _ = scenario
    fail["operation"] = failure
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        run(scenario, tmp_path, activate=True)
    marker = copy.deepcopy(storage.state["intake_state"]["ingress"])
    assert marker["phase"] == "prepared" and storage.state["status"] == "ready"
    assert len(storage.files) == 1 and len(exports) == 1
    fail.clear()
    storage.fail_phase = None
    trace.clear()
    result = run(scenario, tmp_path, activate=True, name="retry", now=AT + timedelta(days=1))
    assert result["status"] == "active" and result["seed_sha256"] == marker["seed_sha256"]
    assert "download" in trace and "upload" not in trace and len(exports) == 1
    assert config["url"] == cutover.PRODUCTION_ORIGIN + "/api/telegram"


def test_existing_active_bootstrap_uses_saved_seed_without_reset(scenario, tmp_path):
    storage, _, trace, exports, _, _, _, _ = scenario
    run(scenario, tmp_path, activate=True)
    before = copy.deepcopy(storage.state["intake_state"])
    trace.clear()
    result = run(scenario, tmp_path, activate=True, name="again", now=AT + timedelta(days=2))
    assert result["status"] == "active" and len(exports) == 1
    assert "upload" not in trace and "prepared" not in trace
    marker = storage.state["intake_state"]["ingress"]
    assert marker["seed_sha256"] == before["ingress"]["seed_sha256"]


def test_failed_prepared_cas_never_bootstraps_or_sets_webhook(scenario, tmp_path):
    storage, _, trace, *_ = scenario
    storage.fail_claim = True
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True)
    assert "ingress" not in storage.state["intake_state"]
    assert storage.state["status"] == "ready"
    assert "transfer" not in trace and "setWebhook" not in trace


@pytest.mark.parametrize("failure", ["prepared", "active"])
def test_failed_ready_commit_keeps_busy_and_cannot_automatically_retry(
    scenario, tmp_path, failure,
):
    storage, _, trace, exports, _, _, _, _ = scenario
    storage.fail_phase = failure
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True)
    before = storage.read()
    assert storage.state["status"] == "busy"
    assert len(exports) == 1 and len(storage.files) == 1
    if failure == "prepared":
        assert "ingress" not in storage.state["intake_state"]
        assert "transfer" not in trace and "setWebhook" not in trace
    else:
        assert storage.state["intake_state"]["ingress"]["phase"] == "prepared"
    trace.clear()
    storage.fail_phase = None
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True, name="blocked-retry")
    assert storage.read() == before
    assert "transfer" not in trace and "setWebhook" not in trace


def test_storage_fake_rejects_direct_ready_metadata_write(scenario):
    storage = scenario[0]
    state, sha = storage.read()
    state["generation"] = "illegal-shortcut"
    state["intake_state"]["ingress"] = {"phase": "prepared"}
    with pytest.raises(AssertionError):
        storage.write(state, sha)
    assert storage.read()[0]["generation"] == "initial"


@pytest.mark.parametrize("receiver", [
    {"status": "disabled"}, {"can_send": False}, {"seed_sha256": "c" * 64},
])
def test_receiver_must_confirm_exact_active_seed_before_webhook(scenario, tmp_path, receiver):
    scenario[-2]["receiver"] = receiver
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True)
    assert scenario[0].state["intake_state"]["ingress"]["phase"] == "prepared"
    assert "setWebhook" not in scenario[2]


@pytest.mark.parametrize("webhook", [{"max_connections": 20}, {"url": ""}])
def test_failed_webhook_verification_does_not_activate(scenario, tmp_path, webhook):
    scenario[-2]["verified_webhook"] = webhook
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True)
    assert scenario[0].state["intake_state"]["ingress"]["phase"] == "prepared"


def test_corrupt_saved_seed_does_not_bootstrap_on_retry(scenario, tmp_path):
    storage, _, trace, _, _, _, fail, _ = scenario
    fail["operation"] = "transfer"
    with pytest.raises(httpx.HTTPStatusError):
        run(scenario, tmp_path, activate=True)
    digest = storage.state["intake_state"]["ingress"]["seed_sha256"]
    storage.files[digest] = b"corrupt"
    fail.clear()
    trace.clear()
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True, name="corrupt-retry")
    assert "transfer" not in trace and "setWebhook" not in trace


@pytest.mark.parametrize("problem", ["origin", "identity", "foreign_webhook", "unfenced"])
def test_wrong_source_cannot_upload_or_change_webhook(scenario, tmp_path, problem):
    storage, _, trace, _, config, _, fail, _ = scenario
    origin = None
    if problem == "origin":
        origin = "https://foreign.vercel.app"
    elif problem == "identity":
        fail["identity"] = {"username": "another_bot"}
    elif problem == "foreign_webhook":
        config["url"] = "https://foreign.invalid/webhook"
    else:
        storage.state["cutover"]["server_fenced"] = False
    before = storage.read()
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True, url=origin)
    assert storage.read() == before and storage.files == {}
    assert "setWebhook" not in trace and "transfer" not in trace


def test_unresolved_notice_blocks_all_mutations(scenario, tmp_path):
    scenario[-1]["cases"] = [{"status": "choosing", "payload": {
        "reader_notices": {"menu": {"status": "uncertain"}}}}]
    assert run(scenario, tmp_path)["status"] == "preflight_blocked_notices"
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True, name="activation")
    assert scenario[0].files == {} and "setWebhook" not in scenario[2]


def test_cli_hides_exception_urls_and_tokens(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("sys.argv", ["ingress_cutover", "--work", str(tmp_path / "cli")])
    for key, value in {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": cutover.REPO, "GITHUB_REF": "refs/heads/main",
        "EDITORIAL_STORAGE_BACKEND": "ssh", "ACTIONS_CUTOVER_CONFIRMED": "true",
        "TELEGRAM_BOT_TOKEN": TOKEN,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cutover.SSHConfig, "from_environment", lambda _: object())
    monkeypatch.setattr(cutover, "SSHStateClient", lambda _: object())

    def fail(*args, **kwargs):
        raise RuntimeError(f"https://api.telegram.org/bot{TOKEN}/getMe {BRIDGE} {SECRET}")

    monkeypatch.setattr(cutover, "execute", fail)
    with pytest.raises(SystemExit) as error:
        cutover.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"status": "blocked", "error_type": "RuntimeError"}
    assert output.err == ""
    assert all(secret not in output.out for secret in (TOKEN, BRIDGE, SECRET))


@pytest.fixture
def rollback_scenario(scenario, tmp_path, monkeypatch):
    run(scenario, tmp_path, activate=True, name="activation")
    storage = scenario[0]
    evidence_state = copy.deepcopy(storage.state)
    evidence_state["run_id"] = "123456789"
    evidence_state["started_at"] = (AT + timedelta(seconds=2)).isoformat()
    evidence = canonical_json({"version": 1, "expected_busy_sha256": "b" * 64,
                               "state": evidence_state}).encode()
    checkpoint = hashlib.sha256(evidence).hexdigest()
    storage.files[checkpoint] = evidence
    scenario[2].clear()
    scenario[5].clear()

    def quiesce(seconds):
        assert seconds == 45
        assert storage.state["status"] == "ready"
        assert storage.state["intake_state"]["ingress"]["phase"] == "prepared"
        assert storage.state["ingress_rollback"]["phase"] == "prepared"
        scenario[2].append("quiesced")

    monkeypatch.setattr(cutover.time, "sleep", quiesce)
    return scenario, checkpoint


def rollback(fixture, tmp_path, name="rollback", **kwargs):
    scenario, checkpoint = fixture
    return cutover.rollback_empty(
        scenario[0], cutover.PRODUCTION_ORIGIN, tmp_path / name, scenario[1], TOKEN, BRIDGE,
        kwargs.pop("expected_state_sha256", "d" * 64),
        kwargs.pop("activation_checkpoint_sha256", checkpoint),
        kwargs.pop("activation_run_id", "123456789"), now=AT + timedelta(hours=1), **kwargs,
    )


def test_rollback_empty_preserves_updates_and_fences_until_quiescent(rollback_scenario, tmp_path):
    scenario, checkpoint = rollback_scenario
    storage, _, trace, *_ = scenario
    before = copy.deepcopy(storage.state)
    previous_files = set(storage.files)
    result = rollback(rollback_scenario, tmp_path)
    assert result["status"] == "polling_restored"
    assert "ingress" not in storage.state["intake_state"]
    assert storage.state["intake_state"] == {
        key: value for key, value in before["intake_state"].items() if key != "ingress"
    }
    assert storage.state["snapshot"] == before["snapshot"]
    assert storage.state["ingress_rollback"]["phase"] == "complete"
    assert storage.state["ingress_rollback"]["activation_checkpoint_sha256"] == checkpoint
    assert previous_files <= set(storage.files)
    assert trace == ["getMe", "download", "getWebhookInfo", "transfer", "audit:prepared",
                     "busy:active", "prepared", "deleteWebhook", "getWebhookInfo", "quiesced",
                     "transfer", "audit:complete", "busy:prepared", "polling"]
    audits = [json.loads(value) for key, value in storage.files.items()
              if key not in previous_files]
    assert {value["phase"] for value in audits} == {"prepared", "complete"}
    assert all(secret not in json.dumps(audits) for secret in (TOKEN, BRIDGE, SECRET))


@pytest.mark.parametrize("problem", [
    "cursor", "wrong_etag", "new_upload", "different_uploads", "metadata_unavailable",
    "checkpoint_hash", "checkpoint_run", "activation_prepared", "different_snapshot",
])
def test_rollback_empty_rejects_unproven_state_before_mutation(
    rollback_scenario, tmp_path, problem,
):
    scenario, _ = rollback_scenario
    storage, _, trace, _, _, _, fail, _ = scenario
    args = {}
    if problem == "cursor":
        storage.state["intake_state"]["serverless_sequence"] = 1
    elif problem == "wrong_etag":
        args["expected_state_sha256"] = "f" * 64
    elif problem == "new_upload":
        fail["metadata_responses"] = [{"state": {
            "uploaded_at": (AT + timedelta(minutes=5)).isoformat()}}]
    elif problem == "different_uploads":
        fail["metadata_responses"] = [{"config": {
            "uploaded_at": (AT - timedelta(seconds=1)).isoformat()}}]
    elif problem == "metadata_unavailable":
        fail["metadata_error"] = True
    elif problem == "checkpoint_hash":
        storage.files[rollback_scenario[1]] += b"corrupt"
    elif problem == "checkpoint_run":
        args["activation_run_id"] = "999999999"
    elif problem == "activation_prepared":
        storage.state["intake_state"]["ingress"]["phase"] = "prepared"
    else:
        storage.state["intake_state"]["ingress"]["seed_snapshot_sha256"] = "f" * 64
    before = storage.read()
    files = copy.deepcopy(storage.files)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        rollback(rollback_scenario, tmp_path, **args)
    assert storage.read() == before and storage.files == files
    assert "deleteWebhook" not in trace and "busy:active" not in trace


@pytest.mark.parametrize("failure", ["deleteWebhook", "changed_state", "changed_config"])
def test_rollback_failure_keeps_prepared_marker_and_blocks_activation(
    rollback_scenario, tmp_path, failure,
):
    scenario = rollback_scenario[0]
    storage, _, trace, _, _, _, fail, _ = scenario
    if failure == "deleteWebhook":
        fail["operation"] = "deleteWebhook"
    else:
        name = "state" if failure == "changed_state" else "config"
        fail["metadata_responses"] = [{}, {name: {"etag_sha256": "f" * 64}}]
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        rollback(rollback_scenario, tmp_path)
    assert storage.state["status"] == "ready"
    assert storage.state["ingress_rollback"]["phase"] == "prepared"
    assert storage.state["intake_state"]["ingress"]["phase"] == "prepared"
    trace.clear()
    fail.clear()
    with pytest.raises(ValueError, match="требует отдельной миграции"):
        run(scenario, tmp_path, activate=True, name="wrong-activation")
    assert "setWebhook" not in trace


def test_prepared_rollback_retry_uses_stored_evidence(rollback_scenario, tmp_path):
    scenario = rollback_scenario[0]
    scenario[6]["operation"] = "deleteWebhook"
    with pytest.raises(httpx.HTTPStatusError):
        rollback(rollback_scenario, tmp_path)
    marker = copy.deepcopy(scenario[0].state["ingress_rollback"])
    scenario[6].clear()
    scenario[2].clear()
    assert rollback(rollback_scenario, tmp_path, name="retry")["status"] == "polling_restored"
    assert "busy:active" not in scenario[2]
    assert scenario[0].state["ingress_rollback"]["request_audit_sha256"] == marker[
        "request_audit_sha256"]
    scenario[2].clear()
    assert rollback(rollback_scenario, tmp_path, name="completed")["already_complete"] is True
    assert scenario[2] == ["getMe", "getWebhookInfo"]
    with pytest.raises(ValueError, match="требует отдельной миграции"):
        run(scenario, tmp_path, activate=True, name="reactivation-blocked")
    assert "setWebhook" not in scenario[2]


def test_rollback_final_ready_failure_never_clears_busy_fence(rollback_scenario, tmp_path):
    storage = rollback_scenario[0][0]
    storage.fail_phase = "polling"
    with pytest.raises(ValueError):
        rollback(rollback_scenario, tmp_path)
    assert storage.state["status"] == "busy"
    assert storage.state["ingress_rollback"]["phase"] == "prepared"
    assert storage.state["intake_state"]["ingress"]["phase"] == "prepared"
    before = storage.read()
    with pytest.raises(ValueError, match="canonical state not ready"):
        rollback(rollback_scenario, tmp_path, name="busy-retry")
    assert storage.read() == before


@pytest.mark.parametrize("invalid", [None, "status", "conclusion", "event", "head_branch", "path"])
def test_rollback_workflow_checks_successful_manual_main_activation(monkeypatch, invalid):
    workflow = yaml.safe_load((Path(__file__).parents[1] /
                               ".github/workflows/ingress-cutover.yml").read_text())
    job = workflow["jobs"]["cutover"]
    assert job["concurrency"] == {
        "group": "canonical-editorial-state", "cancel-in-progress": False, "queue": "max",
    }
    steps = job["steps"]
    verify = next(step for step in steps if "EXPECTED_SHA" in step.get("env", {}))
    runtime = next(step for step in steps if "STORAGE_PRIVATE_KEY" in step.get("env", {}))
    assert steps.index(verify) < steps.index(runtime)
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    for key, value in {
        "EXPECTED_SHA": "f" * 40, "CUTOVER_OPERATION": "rollback-empty",
        "EXPECTED_STATE_SHA256": "d" * 64, "ACTIVATION_CHECKPOINT_SHA256": "a" * 64,
        "ACTIVATION_RUN_ID": "123456789",
    }.items():
        monkeypatch.setenv(key, value)
    activation = {
        "id": 123456789, "status": "completed", "conclusion": "success",
        "event": "workflow_dispatch", "head_branch": "main",
        "path": ".github/workflows/ingress-cutover.yml",
    }
    if invalid is not None:
        activation[invalid] = "untrusted"

    def api(command):
        assert command[:2] == ["gh", "api"]
        if command[-1].endswith("git/ref/heads/main"):
            value = {"object": {"sha": "f" * 40}}
        elif "head_sha=" in command[-1]:
            value = {"workflow_runs": [{"path": ".github/workflows/ci.yml", "head_branch": "main",
                                       "event": "push", "status": "completed",
                                       "conclusion": "success"}]}
        else:
            assert command[-1].endswith("actions/runs/123456789")
            value = activation
        return json.dumps(value).encode()

    monkeypatch.setattr(subprocess, "check_output", api)
    script = verify["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    if invalid is None:
        exec(compile(script, "<rollback-workflow-test>", "exec"), {})
    else:
        with pytest.raises(AssertionError, match="unverified activation"):
            exec(compile(script, "<rollback-workflow-test>", "exec"), {})
