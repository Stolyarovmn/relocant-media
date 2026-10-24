from pathlib import Path

WORKFLOW = Path(".github/workflows/editorial-primary.yml")
COMPOSE = Path("docker-compose.yml")
ENV_EXAMPLE = Path(".env.example")


def test_jev_is_wired_through_editorial_actions_like_gemini() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}" in workflow
    assert "JEV_ENABLED: ${{ vars.JEV_ENABLED || 'false' }}" in workflow
    assert "TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}" in workflow
    assert "if: env.JEV_ENABLED == 'true'" in workflow
    assert "--shadow-duplicates" in workflow
    assert "jev-shadow-duplicates.json" in workflow
    assert "continue-on-error: true" in workflow


def test_server_runtime_does_not_own_jev_credentials() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "JEV_ENABLED:" not in compose
    assert "TYPESAFE_API_KEY:" not in compose
    assert "JEV_ENABLED=" not in env_example
    assert "TYPESAFE_API_KEY=" not in env_example
