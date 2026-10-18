from typing import Any

from relocant_media.ai.gemini import GeminiStructuredProvider


class FakeInteraction:
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
