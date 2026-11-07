import socket

import httpx
import pytest
from google import genai
from google.genai._gaos.utils import retries

from relocant_media.ai.gemini import GeminiStructuredProvider
from relocant_media.ai.gemini_process import GeminiHTTPRejected, _trace_response


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("protected", [False, True])
def test_normalized_retry_backoff_reproduced_and_prevented(monkeypatch, status, protected):
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    calls, sleeps = [], []
    def handle(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"code": status, "message": "offline"}},
                              headers={"Retry-After": "600"}, request=request)
    def sleep(seconds):
        sleeps.append(seconds)
        raise RuntimeError("OFFLINE_BACKOFF_STOP")
    monkeypatch.setattr(retries.time, "sleep", sleep)
    hooks = {"response": [_trace_response]} if protected else {}
    with genai.Client(api_key="offline", http_options={
        "client_args": {"transport": httpx.MockTransport(handle), "event_hooks": hooks},
        "retry_options": {"attempts": 0},
    }) as client:
        assert client._api_client._http_options.retry_options.attempts == 1
        expected = GeminiHTTPRejected if protected else RuntimeError
        with pytest.raises(expected):
            provider = GeminiStructuredProvider(
                "offline", model="gemini-3.5-flash-lite", client=client)
            provider.generate_json(task="rank", system_instruction="offline", prompt="{}",
                                   response_schema={})
    assert calls == [1]
    assert sleeps == ([] if protected else [600.0])
