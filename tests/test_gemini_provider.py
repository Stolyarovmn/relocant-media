from typing import Any

from relocant_media.ai.gemini import GeminiStructuredProvider


class FakeInteraction:
    status = "completed"
    output_text = '{"ok":true}'


class FakeInteractions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> FakeInteraction:
        self.kwargs = kwargs
        return FakeInteraction()


class FakeClient:
    def __init__(self) -> None:
        self.interactions = FakeInteractions()


def test_gemini_provider_uses_interactions_structured_output() -> None:
    client = FakeClient()
    provider = GeminiStructuredProvider(
        api_key="unused",
        model="gemini-3.8-flash",
        client=client,
    )
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }

    result = provider.generate_json(
        task="test",
        system_instruction="Return structured data.",
        prompt="{}",
        response_schema=schema,
    )

    assert result == '{"ok":true}'
    assert client.interactions.kwargs["model"] == "gemini-3.8-flash"
    assert client.interactions.kwargs["store"] is False
    assert client.interactions.kwargs["response_format"] == {
        "type": "text",
        "mime_type": "application/json",
        "schema": schema,
    }


def test_sdk_call_has_a_wall_clock_budget_across_automatic_continuations():
    client = FakeClient()
    provider = GeminiStructuredProvider("unused", client=client, timeout_seconds=12)
    provider.generate_json(task="rank", system_instruction="Check", prompt="{}", response_schema={})
    assert client.interactions.kwargs["timeout"] == 12
    assert client.interactions.kwargs["automatic_continuation"] is True


def test_unfinished_interaction_is_never_accepted_as_structured_success():
    import pytest

    from relocant_media.ai.provider import StructuredOutputError

    client = FakeClient()
    for status in ("requires_continuation", "incomplete", "failed", None):
        client.interactions.create = lambda status=status, **kwargs: type(
            "Result", (), {"status": status, "output_text": '{"ok":true}'})()
        with pytest.raises(StructuredOutputError, match="did not complete"):
            GeminiStructuredProvider("unused", client=client).generate_json(
                task="rank", system_instruction="Check", prompt="{}", response_schema={})
