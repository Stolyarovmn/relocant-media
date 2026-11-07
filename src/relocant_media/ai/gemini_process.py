"""Bound a complete SDK call in a disposable, credential-scoped subprocess."""

import json
import math
import os
import re
import signal
import subprocess
import sys
from typing import Any, cast

from relocant_media.ai.diagnostics import AI_PHASES

_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,60}")


class GeminiCallTimeout(TimeoutError):
    phase = "unknown"


class GeminiWorkerError(RuntimeError):
    phase = "unknown"


class IsolatedGeminiProvider:
    def __init__(self, api_key: str, *, model: str, timeout_seconds: float = 330) -> None:
        if not api_key or not model.strip():
            raise ValueError("Gemini credentials and model required")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 330:
            raise ValueError("Gemini call deadline must be between zero and 330 seconds")
        self._api_key, self._model, self._timeout = api_key, model, timeout_seconds

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        payload = dict(api_key=self._api_key, model=self._model, task=task,
                       system_instruction=system_instruction, prompt=prompt,
                       response_schema=response_schema,
                       http_timeout_ms=max(1, min(300000, int(self._timeout * 1000))))
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
                   "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        environment = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "relocant_media.ai.gemini_process"],
                input=json.dumps(payload), text=True, encoding="utf-8",
                capture_output=True,
                timeout=self._timeout, env=environment, check=False,
            )
        except subprocess.TimeoutExpired as expired:
            # subprocess.run kills and waits for the child before raising. No thread
            # continues using the provider after the caller releases worker.lock.
            error = GeminiCallTimeout("Gemini call exceeded its total deadline")
            error.phase = _last_phase(expired.stderr)
            raise error from None
        try:
            response = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise GeminiWorkerError("Gemini worker returned invalid output") from None
        if (result.returncode != 0 or not isinstance(response, dict)
                or not isinstance(response.get("output"), str) or not response["output"].strip()):
            # Only the SDK error class name may be repeated: never messages, requests or keys.
            kind = response.get("error_type") if isinstance(response, dict) else None
            suffix = f" ({kind})" if isinstance(kind, str) and _CLASS_NAME.fullmatch(kind) else ""
            worker_error = GeminiWorkerError(
                f"Gemini worker did not return structured text{suffix}")
            worker_error.phase = _last_phase(result.stderr)
            raise worker_error
        return cast(str, response["output"])


def _last_phase(stderr: str | bytes | None) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    phase = "unknown"
    for line in (stderr or "")[-4096:].splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if (isinstance(value, dict) and isinstance(value.get("phase"), str)
                and value["phase"] in AI_PHASES):
            phase = value["phase"]
    return phase


class GeminiSDKDeadlineExceeded(TimeoutError):
    pass


def _deadline_signal(signum: int, frame: object) -> None:
    raise GeminiSDKDeadlineExceeded("Gemini SDK wall-clock deadline exceeded")


def _phase(name: str) -> None:
    print(json.dumps({"phase": name}), file=sys.stderr, flush=True)


def _http_trace(event: str, info: dict[str, Any]) -> None:
    # info contains request/response objects, headers and exception values. Never inspect it.
    suffix = event.split(".", 1)[-1]
    phase = {
        "connect_tcp.started": "http_connect", "start_tls.started": "http_tls",
        "send_request_headers.started": "http_send",
        "receive_response_headers.started": "http_wait_headers",
        "receive_response_body.started": "http_read_body",
        "receive_response_body.complete": "http_body_received",
    }.get(suffix)
    if phase is not None:
        _phase(phase)


class GeminiHTTPRejected(RuntimeError):
    """HTTP failure must not enter the SDK's normalized retry/backoff path."""


def _trace_response(response: Any) -> None:
    status = response.status_code
    if status >= 400:
        phase = "http_error_429" if status == 429 else (
            "http_error_5xx" if status >= 500 else "http_error_4xx")
        _phase(phase)
        raise GeminiHTTPRejected("Gemini HTTP request rejected; automatic retry disabled")


def _trace_request(request: Any) -> None:
    _phase("http_request")
    request.extensions["trace"] = _http_trace


def _worker_main() -> None:
    try:
        _phase("import_sdk")
        from google import genai

        from relocant_media.ai.gemini import GeminiStructuredProvider

        request = json.load(sys.stdin)
        _phase("initialize_client")
        if hasattr(signal, "setitimer"):
            signal.signal(signal.SIGALRM, _deadline_signal)
            signal.setitimer(signal.ITIMER_REAL, request["http_timeout_ms"] / 1000)
        client = genai.Client(api_key=request["api_key"],
                              http_options={"timeout": request["http_timeout_ms"],
                                            "retry_options": {"attempts": 0},
                                            "client_args": {"event_hooks": {
                                                "request": [_trace_request],
                                                "response": [_trace_response]}}})
        try:
            provider = GeminiStructuredProvider(request["api_key"],
                                                model=request["model"], client=client,
                                                timeout_seconds=request["http_timeout_ms"] / 1000)
            _phase("sdk_call")
            output = provider.generate_json(**{k: request[k] for k in (
                "task", "system_instruction", "prompt", "response_schema")})
        finally:
            if hasattr(signal, "setitimer"):
                signal.setitimer(signal.ITIMER_REAL, 0)
            if sys.exc_info()[0] is None:
                _phase("close_client")
            client.close()
        print(json.dumps({"output": output}))
    except Exception as error:
        # Never print exception values, requests, keys, or SDK stderr.
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    _worker_main()
