from typing import Any, Protocol


class StructuredOutputError(RuntimeError):
    pass


class StructuredAIProvider(Protocol):
    def generate_json(
        self,
        *,
        task: str,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> str: ...
