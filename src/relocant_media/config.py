from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://relocant:relocant@postgres:5432/relocant"

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
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


settings = Settings()
