from pathlib import Path

WORKFLOW = Path(".github/workflows/editorial-primary.yml")
RUN_CYCLE = Path("deploy/server/run-cycle.sh")
SERVICE = Path("deploy/server/relocant-editorial.service")
COMPOSE = Path("docker-compose.yml")
ENV_EXAMPLE = Path(".env.example")


def test_jev_is_wired_to_canonical_server_runtime() -> None:
    script = RUN_CYCLE.read_text(encoding="utf-8")
    service = SERVICE.read_text(encoding="utf-8")

    assert not WORKFLOW.exists()
    assert "EnvironmentFile=/etc/relocant-media.env" in service
    assert '${JEV_ENABLED:-false}' in script
    assert "relocant_media.ops.server_runtime" in script
    assert "relocant_media.ops.newsroom_status" in script
    assert "--database" in script
    assert "--shadow-duplicates" in script
    assert "jev-shadow-duplicates.json" in script
    assert '"status" -eq 0' in script
    assert '"status":"unavailable"' in script


def test_staging_compose_does_not_own_production_jev_credentials() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    env_example = ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "JEV_ENABLED:" not in compose
    assert "TYPESAFE_API_KEY:" not in compose
    assert "JEV_ENABLED=" not in env_example
    assert "TYPESAFE_API_KEY=" not in env_example
