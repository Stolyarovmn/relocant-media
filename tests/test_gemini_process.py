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

ARGS = dict(task="rank", system_instruction="Check", prompt="{}", response_schema={})


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
        assert data == dict(api_key="test-key", model="test-model", http_timeout_ms=300000, **ARGS)
        assert kwargs["capture_output"] is True
        assert kwargs["timeout"] == 330
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


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 331])
def test_invalid_deadline_rejected(value):
    with pytest.raises(ValueError):
        IsolatedGeminiProvider("test-key", model="test-model", timeout_seconds=value)


def test_worker_error_names_only_the_sdk_error_class(monkeypatch):
    out = json.dumps({"error_type": "ClientError", "message": "SECRET api-key=abc"})
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, out))
    with pytest.raises(GeminiWorkerError) as error:
        IsolatedGeminiProvider("test-key", model="test-model").generate_json(**ARGS)
    assert str(error.value).endswith("(ClientError)")
    assert "SECRET" not in str(error.value) and "abc" not in str(error.value)


def test_a_worker_error_class_that_is_not_a_plain_name_is_not_repeated(monkeypatch):
    out = json.dumps({"error_type": "x" * 200 + " key=abc"})
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, out))
    with pytest.raises(GeminiWorkerError) as error:
        IsolatedGeminiProvider("test-key", model="test-model").generate_json(**ARGS)
    assert "abc" not in str(error.value) and len(str(error.value)) < 120


@pytest.mark.parametrize("deadline,expected_ms", [(330, 300000), (300, 300000), (.2, 200)])
def test_http_deadline_tracks_shorter_process_limit(monkeypatch, deadline, expected_ms):
    def run(argv, **kwargs):
        assert kwargs["timeout"] == deadline
        assert json.loads(kwargs["input"])["http_timeout_ms"] == expected_ms
        return subprocess.CompletedProcess(argv, 0, json.dumps({"output": "{}"}))
    monkeypatch.setattr(subprocess, "run", run)
    IsolatedGeminiProvider("test-key", model="test-model", timeout_seconds=deadline).generate_json(
        **ARGS)


def test_worker_applies_http_deadline_without_sdk_retries(monkeypatch, capsys):
    import io
    from types import SimpleNamespace

    from google import genai

    from relocant_media.ai.gemini_process import _trace_request, _trace_response, _worker_main

    seen = []
    def client(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(interactions=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(output_text="{}", status="completed")),
            close=lambda: seen.append("closed"))
    monkeypatch.setattr(genai, "Client", client)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(dict(
        api_key="test-key", model="test-model", http_timeout_ms=300000, **ARGS))))
    _worker_main()
    assert seen == [{"api_key": "test-key", "http_options": {
        "timeout": 300000, "retry_options": {"attempts": 0},
        "client_args": {"event_hooks": {"request": [_trace_request],
                                       "response": [_trace_response]}}}}, "closed"]
    assert json.loads(capsys.readouterr().out) == {"output": "{}"}


@pytest.mark.skipif(sys.platform == "win32", reason="Linux worker signal deadline")
def test_worker_wall_clock_interrupts_blocked_sdk_without_network():
    program = (
        "import signal,time; "
        "from relocant_media.ai.gemini_process import _deadline_signal; "
        "signal.signal(signal.SIGALRM,_deadline_signal); "
        "signal.setitimer(signal.ITIMER_REAL,.1); time.sleep(30)"
    )
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, timeout=3)
    assert result.returncode != 0
    assert b"GeminiSDKDeadlineExceeded" in result.stderr
    assert time.monotonic() - started < 3


def test_phase_diagnostic_is_allowlisted():
    from relocant_media.ai.gemini_process import _last_phase

    assert _last_phase(b'SECRET\n{"phase":"sdk_call"}\n{"phase":"SECRET"}') == "sdk_call"


def test_transport_trace_never_emits_request_content(capsys):
    from relocant_media.ai.gemini_process import _http_trace, _last_phase

    _http_trace("http11.receive_response_headers.started", {"headers": "SECRET"})
    _http_trace("http11.receive_response_body.started", {"body": "PRIVATE"})
    _http_trace("SECRET.unknown", {"url": "SECRET"})
    output = capsys.readouterr().err
    assert "SECRET" not in output and "PRIVATE" not in output
    assert _last_phase(output) == "http_read_body"


def test_real_sdk_uses_instrumented_transport_without_network(capsys, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, "connect",
                        lambda *args: pytest.fail("network forbidden"))
    import httpx
    from google import genai

    from relocant_media.ai.gemini import GeminiStructuredProvider
    from relocant_media.ai.gemini_process import _trace_request

    calls = []
    def handle(request):
        calls.append(1)
        request.extensions["trace"]("http11.receive_response_headers.started", {})
        raise RuntimeError("OFFLINE_SENTINEL")
    with genai.Client(api_key="offline-test", http_options={
        "client_args": {"transport": httpx.MockTransport(handle),
                        "event_hooks": {"request": [_trace_request]}},
        "retry_options": {"attempts": 0},
    }) as client:
        with pytest.raises(RuntimeError, match="OFFLINE_SENTINEL"):
            GeminiStructuredProvider("offline-test", model="gemini-3.5-flash-lite",
                                     client=client).generate_json(**ARGS)
    assert calls == [1]
    assert '"http_wait_headers"' in capsys.readouterr().err
