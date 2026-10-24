import json
import subprocess
import sys
import time

import pytest

from relocant_media.ai.gemini_process import (
    GeminiCallTimeout,
    GeminiWorkerError,
    IsolatedGeminiProvider,
)

ARGS = dict(task="rank", system_instruction="Проверка", prompt="{}", response_schema={})


def test_hung_sdk_worker_is_killed_within_total_deadline(monkeypatch):
    original = subprocess.run

    def hung(argv, **kwargs):
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(subprocess, "run", hung)
    started = time.monotonic()
    with pytest.raises(GeminiCallTimeout):
        IsolatedGeminiProvider("test-key", model="test-model", timeout_seconds=.2).generate_json(
            **ARGS)
    assert time.monotonic() - started < 5


def test_worker_receives_only_provider_credentials_via_stdin(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "never-forward-this")
    monkeypatch.setenv("DATABASE_URL", "never-forward-db")
    monkeypatch.setenv("GEMINI_API_KEY", "also-not-in-environment")
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        assert "test-key" not in " ".join(argv)
        assert all(k not in kwargs["env"] for k in (
            "TELEGRAM_BOT_TOKEN", "DATABASE_URL", "GEMINI_API_KEY"))
        data = json.loads(kwargs["input"])
        assert data == dict(api_key="test-key", model="test-model", **ARGS)
        assert kwargs["stderr"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0, json.dumps({"output": '{"ok":true}'}))

    monkeypatch.setattr(subprocess, "run", run)
    provider = IsolatedGeminiProvider("test-key", model="test-model")
    assert provider.generate_json(**ARGS) == '{"ok":true}'
    assert len(seen) == 1


@pytest.mark.parametrize("code,output", [(1, '{"error_type":"ValueError"}'),
                                        (0, "secret-error-message"),
                                        (0, '{"output":""}')])
def test_worker_failures_do_not_leak_outputs(monkeypatch, code, output):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, code, output))
    with pytest.raises(GeminiWorkerError) as error:
        IsolatedGeminiProvider("test-key", model="test-model").generate_json(**ARGS)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 121])
def test_invalid_deadline_rejected(value):
    with pytest.raises(ValueError):
        IsolatedGeminiProvider("test-key", model="test-model", timeout_seconds=value)
