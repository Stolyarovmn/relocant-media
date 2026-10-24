"""Bound a complete SDK call in a disposable, credential-scoped subprocess."""

import json
import math
import os
import subprocess
import sys
from typing import Any, cast


class GeminiCallTimeout(TimeoutError):
    pass


class GeminiWorkerError(RuntimeError):
    pass


class IsolatedGeminiProvider:
    def __init__(self, api_key: str, *, model: str, timeout_seconds: float = 45) -> None:
        if not api_key or not model.strip():
            raise ValueError("Gemini credentials and model required")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError("Gemini call deadline must be between zero and 120 seconds")
        self._api_key, self._model, self._timeout = api_key, model, timeout_seconds

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        payload = dict(api_key=self._api_key, model=self._model, task=task,
                       system_instruction=system_instruction, prompt=prompt,
                       response_schema=response_schema)
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
                   "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        environment = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "relocant_media.ai.gemini_process"],
                input=json.dumps(payload), text=True, encoding="utf-8",
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=self._timeout, env=environment, check=False,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run kills and waits for the child before raising. No thread
            # continues using the provider after the caller releases worker.lock.
            raise GeminiCallTimeout("Gemini call exceeded its total deadline") from None
        try:
            response = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise GeminiWorkerError("Gemini worker returned invalid output") from None
        if (result.returncode != 0 or not isinstance(response, dict)
                or not isinstance(response.get("output"), str) or not response["output"].strip()):
            raise GeminiWorkerError("Gemini worker did not return structured text")
        return cast(str, response["output"])


def _worker_main() -> None:
    try:
        from google import genai

        from relocant_media.ai.gemini import GeminiStructuredProvider

        request = json.load(sys.stdin)
        client = genai.Client(api_key=request["api_key"],
                              http_options={"timeout": 45000, "retry_options": {"attempts": 0}})
        try:
            provider = GeminiStructuredProvider(request["api_key"],
                                                model=request["model"], client=client)
            output = provider.generate_json(**{k: request[k] for k in (
                "task", "system_instruction", "prompt", "response_schema")})
        finally:
            client.close()
        print(json.dumps({"output": output}))
    except Exception as error:
        # Never print exception values, requests, keys, or SDK stderr.
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    _worker_main()
