import asyncio
from pathlib import Path

import pytest

from relocant_media.ops import media_cli


@pytest.mark.parametrize("environment", ["prod", "production", "", "test"])
def test_media_cli_staging_required_before_io(
    monkeypatch: pytest.MonkeyPatch, environment: str,
) -> None:
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setattr(media_cli, "create_engine", lambda _: pytest.fail("database opened"))
    with pytest.raises(ValueError, match="APP_ENV=staging"):
        asyncio.run(media_cli.run(Path("absent"), Path("absent"), Path("absent"), "fake", False))


@pytest.mark.parametrize("transport,send", [("bot-api", False), ("fake", True)])
def test_real_media_delivery_requires_matching_explicit_flags(
    monkeypatch: pytest.MonkeyPatch, transport: str, send: bool,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValueError, match="--send-staging"):
        asyncio.run(media_cli.run(Path("absent"), Path("absent"), Path("absent"), transport, send))
