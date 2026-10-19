import pytest
from sqlalchemy import create_engine, inspect

from relocant_media.ops import actions_cycle


def test_cycle_persists_sqlite_without_ai_or_delivery(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")

    async def collect(*args):
        return {"failures": 0, "raw_items_written": 0}

    monkeypatch.setattr(actions_cycle, "collect", collect)
    for _ in range(2):
        result = actions_cycle.run(analyze=False)
        assert result["publication"] == "Не публиковалось"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        assert "raw_item" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize("environment", ["prod", "production", "dev", ""])
def test_cycle_requires_staging(monkeypatch, environment):
    monkeypatch.setenv("APP_ENV", environment)
    with pytest.raises(ValueError, match="staging"):
        actions_cycle.run(analyze=False)


def test_cycle_rejects_external_database(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example.invalid/db")
    with pytest.raises(ValueError, match="SQLite"):
        actions_cycle.run(analyze=False)
