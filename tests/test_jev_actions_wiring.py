from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/jev-smoke.yml")
RUN_CYCLE = Path("deploy/server/run-cycle.sh")
SERVICE = Path("deploy/server/relocant-editorial.service")
COMPOSE = Path("docker-compose.yml")
ENV_EXAMPLE = Path(".env.example")


def test_existing_server_observer_remains_optional_and_fail_open() -> None:
    script = RUN_CYCLE.read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/relocant-media.env" in service
    assert '${JEV_ENABLED:-false}' in script
    assert "relocant_media.ops.server_runtime" in script
    assert "relocant_media.ops.newsroom_status" in script
    assert "--database" in script
    assert "--shadow-duplicates" in script
    assert "jev-shadow-duplicates.json" in script
    assert '"$status" -eq 0' in script
    assert '"status":"unavailable"' in script


def test_actions_smoke_has_no_newsroom_or_publication_credentials() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request" not in workflow["on"]
    assert "schedule" not in workflow["on"]
    assert workflow["on"]["push"]["branches"] == ["main"]
    steps = workflow["jobs"]["smoke"]["steps"]
    step = next(step for step in steps if "env" in step)
    assert "relocant_media.ops.jev_smoke" in step["run"]
    assert step["env"]["JEV_ENABLED"] == "${{ vars.JEV_ENABLED || 'false' }}"
    assert step["env"]["TYPESAFE_API_KEY"] == (
        "${{ vars.JEV_ENABLED == 'true' && secrets.TYPESAFE_API_KEY || '' }}"
    )
    for name in ("TELEGRAM_RUNTIME_ENABLED", "TELEGRAM_MTPROTO_ENABLED",
                 "EDITORIAL_SEND_ENABLED"):
        assert step["env"][name] == "false"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets.DATABASE_URL" not in text
    assert "secrets.GEMINI_API_KEY" not in text
    assert "secrets.TELEGRAM" not in text


def test_staging_compose_does_not_own_production_jev_credentials() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "JEV_ENABLED:" not in compose
    assert "TYPESAFE_API_KEY:" not in compose
    assert "JEV_ENABLED=" not in env_example
    assert "TYPESAFE_API_KEY=" not in env_example
