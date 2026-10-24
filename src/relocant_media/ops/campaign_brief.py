"""Generate a proposal from the shared brief; no Telegram or advertising clients."""

import os
from pathlib import Path
from typing import Any, Protocol

from google import genai


class Interactions(Protocol):
    def create(self, **kwargs: Any) -> object: ...


def generate_proposal(interactions: Interactions, *, model: str, brief: str) -> str:
    if not model.strip() or not brief.strip():
        raise ValueError("Model and brief are required")
    response = interactions.create(model=model, input=brief, store=False)
    output = getattr(response, "output_text", None)
    if not isinstance(output, str) or not output.strip():
        raise ValueError("Gemini returned no proposal text")
    return output


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY secret is missing; no API request made")
    model = os.environ.get("GEMINI_MODEL", "")
    if not model:
        raise SystemExit("GEMINI_MODEL is required")
    brief = Path("prompts/belgrade-acquisition-brief.md").read_text(encoding="utf-8")
    try:
        client = genai.Client(api_key=api_key)
        output = generate_proposal(client.interactions, model=model, brief=brief)
    except Exception:
        # SDK errors can contain request data; never dump credentials or raw exceptions.
        raise SystemExit("Gemini request failed; no proposal was generated") from None
    output = output.replace(api_key, "[REDACTED]")
    target = Path("campaign-output")
    target.mkdir(exist_ok=True)
    (target / "gemini-proposal.md").write_text(output, encoding="utf-8")
    (target / "brief.md").write_text(brief, encoding="utf-8")
    (target / "provenance.txt").write_text(
        f"model={model}\ncommit={os.environ.get('GITHUB_SHA', 'local')}\n",
        encoding="utf-8",
    )
    print("Gemini proposal saved; treat model claims as unverified suggestions.")
    # The public brief contains no private audience data; show only this generated proposal.
    print(output)


if __name__ == "__main__":
    main()
