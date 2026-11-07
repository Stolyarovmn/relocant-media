import json
import subprocess

import pytest

from relocant_media.ai import gemini_process as worker
from relocant_media.ai.quota_diagnostics import quota_details, safe_quota


def test_extracts_only_structured_quota_coordinates():
    payload = {"error": {"message": "SECRET project=123", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
            "quotaValue": "15", "quotaDimensions": {"project": "SECRET"}}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "60s"}]}}
    result = quota_details(payload)
    assert result == {"retry_seconds": 60, "limits": [{
        "metric": "generate_content_free_tier_requests", "limit": 15, "period": "minute"}]}
    assert "SECRET" not in json.dumps(result) and "project" not in json.dumps(result)
    assert quota_details({"error": {"message": "limit=999 SECRET"}}) == {}
    assert safe_quota({"limits": [{"metric": "generate_content_x", "limit": 5,
                                  "period": {"SECRET": 1}}]}) == {}


def test_only_server_reported_cooldown_blocks_local_provider(monkeypatch):
    calls = []
    clock = [100.0]
    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 1, json.dumps({
                "error_type": "GeminiHTTPRejected", "quota": {"retry_seconds": 60}}),
                '{"phase":"http_error_429"}')
        return subprocess.CompletedProcess(args, 0, '{"output":"{}"}', '')
    monkeypatch.setattr(worker.subprocess, "run", run)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    provider = worker.IsolatedGeminiProvider("offline", model="offline")
    args = dict(task="rank", system_instruction="offline", prompt="{}", response_schema={})
    for phase in ("http_error_429", "quota_cooldown"):
        with pytest.raises(worker.GeminiWorkerError) as error:
            provider.generate_json(**args)
        assert error.value.phase == phase
    assert calls == [1]
    clock[0] = 161
    assert provider.generate_json(**args) == "{}"
    assert calls == [1, 1]


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="Linux diagnostic read bound")
def test_quota_body_diagnostic_is_bounded_and_restores_alarm():
    import signal
    import time

    class Response:
        status_code = 429
        headers = {"retry-after": "10"}
        def iter_bytes(self, chunk_size):
            time.sleep(30)
            yield b"{}"
    previous = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(worker.GeminiHTTPRejected) as error:
        worker._trace_response(Response())
    assert error.value.quota == {"retry_seconds": 10}
    assert time.monotonic() - started < 4
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_metric_is_retained_without_inventing_missing_limit():
    result = quota_details({"error": {"message": "limit 999", "details": [{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_requests",
            "quotaId": "GenerateRequestsPerDayPerProject"}]}]}})
    assert result == {"limits": [{"metric": "generate_content_requests", "period": "day"}]}


def test_private_quota_report_drops_raw_and_local_cached_data(tmp_path):
    from relocant_media.ops.actions_runtime import private_quota_observation

    records = [{"error_phase": "http_error_429", "quota": {"retry_seconds": 60,
                "message": "SECRET", "project": "PRIVATE"}},
               {"error_phase": "quota_cooldown", "quota": {"retry_seconds": 999}}]
    (tmp_path / "ai-calls.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records), encoding="utf-8")
    result = private_quota_observation(tmp_path)
    assert result["reports"] == [{"retry_seconds": 60}]
    assert "SECRET" not in json.dumps(result) and "PRIVATE" not in json.dumps(result)
