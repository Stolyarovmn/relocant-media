import asyncio
from pathlib import Path

import pytest

from relocant_media.ops import collect_cli


@pytest.mark.parametrize("environment", ["", "prod", "production", "test"])
def test_collect_requires_explicit_staging_before_connection(
    monkeypatch: pytest.MonkeyPatch, environment: str,
) -> None:
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setattr(collect_cli, "create_engine", lambda _: pytest.fail("database opened"))
    with pytest.raises(ValueError, match="APP_ENV=staging"):
        asyncio.run(collect_cli.collect("belgrade", Path("unused"), ("unused",)))


def test_collect_requires_explicit_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        asyncio.run(collect_cli.collect("belgrade", Path("unused"), ("unused",)))
