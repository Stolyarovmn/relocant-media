from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from relocant_media.config import Settings, runtime_configuration_errors


def database_status(db_engine: Engine) -> tuple[bool, str | None]:
    try:
        with db_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as error:
        return False, type(error).__name__
    return True, None


def build_readiness(config: Settings, db_engine: Engine) -> dict[str, object]:
    configuration_errors = runtime_configuration_errors(config)
    database_ready, database_error = database_status(db_engine)
    ready = database_ready and not configuration_errors
    return {
        "status": "ready" if ready else "not_ready",
        "environment": config.app_env,
        "city": config.city_id,
        "telegram_runtime_enabled": config.telegram_runtime_enabled,
        "database": {
            "ready": database_ready,
            "error": database_error,
        },
        "configuration": {
            "ready": not configuration_errors,
            "errors": configuration_errors,
        },
    }
