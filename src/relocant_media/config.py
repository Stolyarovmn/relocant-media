from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    city_id: str = "belgrade"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://relocant:relocant@postgres:5432/relocant"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_webhook_url: str = ""
    telegram_runtime_enabled: bool = False
    telegram_staging_channel_id: str = ""
    telegram_staging_discussion_group_id: str = ""
    telegram_staging_forum_group_id: str = ""

    # Production IDs are intentionally not consumed by the staging runtime.
    telegram_channel_id: str = ""
    telegram_discussion_group_id: str = ""
    telegram_forum_group_id: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    anthropic_api_key: str = ""


def telegram_setup_errors(config: Settings) -> list[str]:
    required = {
        "TELEGRAM_BOT_TOKEN": config.telegram_bot_token,
        "TELEGRAM_WEBHOOK_SECRET": config.telegram_webhook_secret,
        "TELEGRAM_WEBHOOK_URL": config.telegram_webhook_url,
        "TELEGRAM_STAGING_CHANNEL_ID": config.telegram_staging_channel_id,
        "TELEGRAM_STAGING_DISCUSSION_GROUP_ID": config.telegram_staging_discussion_group_id,
        "TELEGRAM_STAGING_FORUM_GROUP_ID": config.telegram_staging_forum_group_id,
    }
    errors = [f"{name} is not configured" for name, value in required.items() if not value]

    if config.telegram_webhook_url and not config.telegram_webhook_url.startswith("https://"):
        errors.append("TELEGRAM_WEBHOOK_URL must use HTTPS")

    pairs = (
        (
            "channel",
            config.telegram_staging_channel_id,
            config.telegram_channel_id,
        ),
        (
            "discussion group",
            config.telegram_staging_discussion_group_id,
            config.telegram_discussion_group_id,
        ),
        (
            "forum group",
            config.telegram_staging_forum_group_id,
            config.telegram_forum_group_id,
        ),
    )
    for label, staging_id, production_id in pairs:
        if staging_id and production_id and staging_id == production_id:
            errors.append(f"staging {label} must not equal production {label}")

    return errors


def runtime_configuration_errors(config: Settings) -> list[str]:
    if not config.database_url:
        return ["DATABASE_URL is not configured"]
    if not config.telegram_runtime_enabled:
        return []

    errors = telegram_setup_errors(config)
    if config.app_env.lower() == "prod":
        errors.append("staging Telegram runtime must not run with APP_ENV=prod")
    return errors


settings = Settings()
