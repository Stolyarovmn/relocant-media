import json
import sys

import httpx
import pytest

from relocant_media.ai import jev
from relocant_media.ops import jev_smoke


def test_disabled_smoke_never_reads_key_or_opens_client(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_ENABLED", "false")
    real_getenv = jev.os.getenv

    def guarded_getenv(name, default=None):
        assert name != "TYPESAFE_API_KEY"
        return real_getenv(name, default)

    monkeypatch.setattr(jev.os, "getenv", guarded_getenv)
    monkeypatch.setattr(jev.httpx, "Client", lambda **kw: pytest.fail("HTTP forbidden"))
    result = jev_smoke.run_smoke(tmp_path / "output")
    assert result["jev_shadow"]["status"] == "disabled"
    assert result["production_benchmark"] is False
    assert result["newsroom_state_accessed"] is False


def test_smoke_uses_shared_adapter_and_only_public_fixed_example(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", "DO-NOT-ACCESS-DATABASE")
    monkeypatch.setenv("GEMINI_API_KEY", "DO-NOT-SEND-GEMINI")
    requests = []
    client = httpx.Client

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-secret"
        payload = json.loads(request.content)
        assert payload["questions"] == {
            "billing": {"type": "noul", "instructions": "Is this message about billing?"},
        }
        assert "charged twice" in payload["state"]["message"]
        assert "DO-NOT" not in request.content.decode()
        return httpx.Response(200, json={
            "model": "jev-test", "answers": {"billing": {"type": "noul", "noul": 0.98}},
            "usage": {"input_tokens": 120, "output_tokens": 12},
        })

    monkeypatch.setattr(jev.httpx, "Client", lambda **kw: client(
        transport=httpx.MockTransport(handle), **kw,
    ))
    output = tmp_path / "output"
    result = jev_smoke.run_smoke(output)
    assert len(requests) == 1
    assert result["jev_shadow"]["status"] == "ok"
    assert result["jev_shadow"]["input_tokens"] == 120
    assert result["jev_shadow"]["output_tokens"] == 12
    assert result["jev_shadow"]["probabilities"] == {"billing": 0.98}
    assert result["telegram_transport"] == "none"
    saved = (output / "manifest.json").read_text()
    assert json.loads(saved) == result
    assert "test-secret" not in saved
    assert "charged twice" not in saved
    with pytest.raises(FileExistsError):
        jev_smoke.run_smoke(output)
    assert len(requests) == 1


def test_missing_secret_is_visible_cli_failure_without_http(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(jev.httpx, "Client", lambda **kw: pytest.fail("HTTP forbidden"))
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["jev_smoke", "--output", str(output)])
    with pytest.raises(SystemExit) as error:
        jev_smoke.main()
    assert error.value.code == 1
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["jev_shadow"]["status"] == "missing_api_key"
