import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

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

    def read(self):
        return copy.deepcopy(self.state), self.sha

    def write(self, state, previous):
        phase = state["intake_state"]["ingress"]["phase"]
        assert previous == self.sha
        if self.fail_phase == phase:
            raise ValueError("conditional write failed")
        self.trace.append(phase)
        self.state = copy.deepcopy(state)
        self.sha += "x"
        return self.sha

    def upload(self, path, folder):
        assert folder == "intake-seed"
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        self.files[digest] = data
        self.trace.append("upload")
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
            assert storage.state["intake_state"]["ingress"]["phase"] in {"prepared", "active"}
            assert payload == {"url": cutover.PRODUCTION_ORIGIN + "/api/telegram",
                               "secret_token": SECRET, "max_connections": 1,
                               "drop_pending_updates": False}
            config.update(url=payload["url"])
            config.update(fail.get("verified_webhook", {}))
            result = True
        elif operation == "transfer":
            assert request.headers["Authorization"] == f"Bearer {BRIDGE}"
            assert storage.state["intake_state"]["ingress"]["phase"] in {"prepared", "active"}
            assert payload["operation"] == "bootstrap"
            assert payload["seed_sha256"] in storage.files
            result = {"status": "ready", "can_send": True,
                      "seed_sha256": payload["seed_sha256"]}
            result.update(fail.get("receiver", {}))
            return httpx.Response(200, json=result)
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
    assert trace == ["getMe", "getChatMember", "getWebhookInfo", "upload", "prepared",
                     "transfer", "setWebhook", "getWebhookInfo", "active"]
    assert storage.state["intake_state"]["attempts"] == 1
    marker = storage.state["intake_state"]["ingress"]
    assert marker["seed_snapshot_sha256"] == "a" * 64
    assert storage.files[marker["seed_sha256"]] == canonical_json(scenario[-1]).encode()
    assert all("allowed_updates" not in payload for _, payload in requests)


@pytest.mark.parametrize("failure", ["transfer", "setWebhook", "active"])
def test_failure_leaves_prepared_fence_and_retry_reuses_exact_seed(scenario, tmp_path, failure):
    storage, _, trace, exports, config, _, fail, _ = scenario
    if failure == "active":
        storage.fail_phase = "active"
    else:
        fail["operation"] = failure
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        run(scenario, tmp_path, activate=True)
    marker = copy.deepcopy(storage.state["intake_state"]["ingress"])
    assert marker["phase"] == "prepared"
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
    storage.fail_phase = "prepared"
    with pytest.raises(ValueError):
        run(scenario, tmp_path, activate=True)
    assert "ingress" not in storage.state["intake_state"]
    assert "transfer" not in trace and "setWebhook" not in trace


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
