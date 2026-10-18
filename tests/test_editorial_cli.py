import pytest

from relocant_media.ops import editorial_cli


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["prod", "production", "PROD"])
async def test_editorial_cli_blocks_production_before_clients(monkeypatch, environment):
    monkeypatch.setattr(editorial_cli.settings, "app_env", environment)
    with pytest.raises(ValueError, match="production"):
        await editorial_cli._run(1, False, "fake")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["dry-run", "fake"])
async def test_editorial_cli_requires_explicit_real_transport(monkeypatch, transport):
    monkeypatch.setattr(editorial_cli.settings, "app_env", "dev")
    with pytest.raises(ValueError, match="requires --transport bot-api"):
        await editorial_cli._run(1, True, transport)
