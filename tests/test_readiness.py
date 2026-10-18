from fastapi.testclient import TestClient

from relocant_media.api.app import app
from relocant_media.config import Settings, runtime_configuration_errors, telegram_setup_errors


def _telegram_ready_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "staging",
        "telegram_runtime_enabled": True,
        "telegram_bot_token": "token",
        "telegram_webhook_secret": "secret",
        "telegram_webhook_url": "https://staging.example.test/telegram/webhook",
        "telegram_staging_channel_id": "-1001",
        "telegram_staging_discussion_group_id": "-1002",
        "telegram_staging_forum_group_id": "-1003",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_runtime_configuration_is_inert_when_telegram_disabled() -> None:
    config = Settings(_env_file=None, telegram_runtime_enabled=False)
    assert runtime_configuration_errors(config) == []


def test_telegram_setup_requires_every_staging_value() -> None:
    config = Settings(_env_file=None, telegram_runtime_enabled=True)
    errors = telegram_setup_errors(config)
    assert "TELEGRAM_BOT_TOKEN is not configured" in errors
    assert "TELEGRAM_WEBHOOK_SECRET is not configured" in errors
    assert "TELEGRAM_WEBHOOK_URL is not configured" in errors
    assert "TELEGRAM_STAGING_CHANNEL_ID is not configured" in errors
    assert "TELEGRAM_STAGING_DISCUSSION_GROUP_ID is not configured" in errors
    assert "TELEGRAM_STAGING_FORUM_GROUP_ID is not configured" in errors


def test_runtime_rejects_production_environment_and_id_collision() -> None:
    config = _telegram_ready_settings(
        app_env="prod",
        telegram_channel_id="-1001",
    )
    errors = runtime_configuration_errors(config)
    assert "staging Telegram runtime must not run with APP_ENV=prod" in errors
    assert "staging channel must not equal production channel" in errors


def test_runtime_requires_https_webhook() -> None:
    config = _telegram_ready_settings(telegram_webhook_url="http://example.test/telegram/webhook")
    assert "TELEGRAM_WEBHOOK_URL must use HTTPS" in runtime_configuration_errors(config)


def test_ready_endpoint_returns_503_for_failed_readiness(monkeypatch: object) -> None:
    def fake_readiness(*_args: object) -> dict[str, object]:
        return {
            "status": "not_ready",
            "database": {"ready": False, "error": "OperationalError"},
            "configuration": {"ready": True, "errors": []},
        }

    monkeypatch.setattr("relocant_media.api.app.build_readiness", fake_readiness)  # type: ignore[attr-defined]
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_ready_endpoint_returns_200_for_ready_state(monkeypatch: object) -> None:
    def fake_readiness(*_args: object) -> dict[str, object]:
        return {
            "status": "ready",
            "database": {"ready": True, "error": None},
            "configuration": {"ready": True, "errors": []},
        }

    monkeypatch.setattr("relocant_media.api.app.build_readiness", fake_readiness)  # type: ignore[attr-defined]
    response = TestClient(app).get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
