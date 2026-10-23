import json

import httpx
import pytest

from relocant_media.ai import jev


def _mock_http(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(
        jev.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def _candidate(raw_id, ownership, title, body):
    return {
        "raw_item_id": raw_id,
        "source": {"ownership": ownership},
        "evidence": {"title": title, "body": body, "url": "https://official.example/item"},
    }


def test_single_switch_disables_all_jev_network_calls(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "false")
    monkeypatch.setenv("TYPESAFE_API_KEY", "unused")
    monkeypatch.setattr(
        jev.httpx, "Client", lambda **kwargs: pytest.fail("Jev must not create HTTP client")
    )
    assert jev.jev_enabled() is False
    assert jev.rank_shadow([_candidate(1, "official", "title", "body")], city="belgrade")[
        "status"
    ] == "disabled"
    assert jev.duplicate_shadow([{"left_title": "a"}], city="belgrade")["status"] == "disabled"


def test_missing_key_does_not_make_network_request(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(
        jev.httpx, "Client", lambda **kwargs: pytest.fail("missing key must not call API")
    )
    result = jev.rank_shadow(
        [_candidate(1, "official", "Public announcement", "Public text")], city="belgrade"
    )
    assert result["status"] == "missing_api_key"
    assert result["decision_effect"] == "none"


def test_rank_batches_public_official_news_and_never_sends_private_text(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key")
    seen = {}

    def handle(request):
        assert str(request.url) == jev.ENDPOINT
        assert request.headers["Authorization"] == "Bearer fake-key"
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "jev-test",
            "answers": {
                "local_0": {"type": "noul", "noul": 0.91},
                "practical_0": {"type": "noul", "noul": 0.76},
            },
            "usage": {"input_tokens": 210, "output_tokens": 5},
        })

    _mock_http(monkeypatch, handle)
    result = jev.rank_shadow(
        [
            _candidate(3, "community", "PRIVATE USER TIP", "PRIVATE USER BODY"),
            _candidate(7, "official", "Transport update", "Road works " + "a" * 2000),
        ],
        city="belgrade",
    )

    assert result["status"] == "ok"
    assert result["evaluated"] == 1
    assert result["decision_effect"] == "none"
    assert result["signals"] == [{
        "raw_item_id": 7,
        "local_relevance_probability": 0.91,
        "practical_value_probability": 0.76,
    }]
    assert result["input_tokens"] == 210
    assert result["output_tokens"] == 5
    assert "PRIVATE" not in json.dumps(seen["payload"])
    assert "https://official.example" not in json.dumps(seen["payload"])
    assert len(seen["payload"]["state"]["candidates"][0]["body"]) == 600
    assert len(seen["payload"]["questions"]) == 2


def test_duplicate_judgments_are_bounded_and_non_authoritative(monkeypatch):
    monkeypatch.setenv("JEV_ENABLED", "yes")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key")
    seen = {}

    def handle(request):
        payload = json.loads(request.content)
        seen["payload"] = payload
        return httpx.Response(200, json={
            "model": "jev-test",
            "answers": {
                key: {"type": "noul", "noul": 0.8}
                for key in payload["questions"]
            },
            "usage": {"input_tokens": 500, "output_tokens": 15},
        })

    _mock_http(monkeypatch, handle)
    pairs = [
        {
            "left_raw_item_id": i,
            "right_raw_item_id": i + 100,
            "left_title": "Road closed",
            "left_body": "Details",
            "right_title": "Road open",
            "right_body": "Different action",
        }
        for i in range(20)
    ]
    result = jev.duplicate_shadow(pairs, city="belgrade")
    assert result["status"] == "ok"
    assert result["evaluated"] == jev.MAX_DUPLICATE_PAIRS == 8
    assert len(result["signals"]) == 8
    assert result["signals"][0]["same_event_probability"] == 0.8
    assert result["decision_effect"] == "none"
    assert len(seen["payload"]["questions"]) == 8


@pytest.mark.parametrize("body", [
    {"error": "do not leak sensitive upstream body"},
    {"answers": {"local_0": {"type": "noul", "noul": True}}},
])
def test_bad_api_response_fails_open_without_leaking_payload(monkeypatch, body):
    monkeypatch.setenv("JEV_ENABLED", "true")
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-key")

    def handle(request):
        if "error" in body:
            return httpx.Response(429, json=body)
        return httpx.Response(200, json=body)

    _mock_http(monkeypatch, handle)
    result = jev.rank_shadow(
        [_candidate(2, "official", "Public title", "Public body")], city="belgrade"
    )
    assert result["status"] == "unavailable"
    assert result["decision_effect"] == "none"
    assert "fake-key" not in json.dumps(result)
    assert "sensitive upstream body" not in json.dumps(result)
