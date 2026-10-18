from typing import Any, Protocol, cast

from google import genai

from relocant_media.ai.provider import StructuredOutputError


class _InteractionsResource(Protocol):
    def create(self, **kwargs: Any) -> object: ...


class _GeminiClient(Protocol):
    interactions: _InteractionsResource


class GeminiStructuredProvider:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.8-flash",
        client: object | None = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise ValueError("Gemini API key is required")
            client = genai.Client(api_key=api_key)
        self._client = cast(_GeminiClient, client)
        self._model = model

    def generate_json(
        self,
        *,
        task: str,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        response_format: dict[str, Any] = {
            "type": "text",
            "mime_type": "application/json",
            "schema": response_schema,
        }
        interaction = self._client.interactions.create(
            model=self._model,
            input=prompt,
            system_instruction=system_instruction,
            response_format=response_format,
            store=False,
            labels={"component": "editorial", "task": task},
        )
        output_text = getattr(interaction, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise StructuredOutputError(f"Gemini returned empty structured output for {task}")
        return output_text
