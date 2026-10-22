from pathlib import Path

import pytest
from test_production_media_delivery import (
    _preview,
    _stored_package,
    _write_release,
)
from test_production_media_delivery import reviewed as _reviewed

from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.production_media import prepare_production_media
from relocant_media.services.production_media_install import install_production_media
from relocant_media.storage.models import EditorialRun

reviewed = _reviewed


def _source_paths(root: Path, run: EditorialRun) -> tuple[Path, Path]:
    bundle = root / str(run.raw_item_id)
    return bundle / "release.json", bundle / "assets"


@pytest.mark.asyncio
async def test_installs_reviewed_bundle_atomically_without_telegram(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    package = _stored_package(run)
    caption = await _preview(session, run.raw_item_id)
    source = tmp_path / "source"
    release = _write_release(source, run, package, caption)
    release_path, assets = _source_paths(source, run)
    destination = tmp_path / "inbox"

    result = await install_production_media(
        session,
        release_path=release_path,
        assets=assets,
        root=destination,
    )

    assert result["state"] == "installed"
    assert result["asset_count"] == 2
    assert result["telegram_io_performed"] is False
    final = destination / str(run.raw_item_id)
    assert (final / "release.json").read_bytes() == release_path.read_bytes()
    assert {path.name for path in (final / "assets").iterdir()} == {
        "image-0.png",
        "image-1.png",
    }
    assert not [path for path in destination.iterdir() if path.name.startswith(".install-")]

    prepared = prepare_production_media(
        raw_item_id=run.raw_item_id,
        post_id=run.post_id,
        editorial_package_sha256=release.editorial_package_sha256,
        target_channel_id=APPROVED_CHANNEL_ID,
        editorial_text=caption,
        root=destination,
    )
    assert prepared is not None and len(prepared.uploads) == 2


@pytest.mark.asyncio
async def test_invalid_rights_leave_no_production_bundle(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    package = _stored_package(run)
    caption = await _preview(session, run.raw_item_id)
    source = tmp_path / "source"
    _write_release(source, run, package, caption, asset_count=1, rights_verified=False)
    release_path, assets = _source_paths(source, run)
    destination = tmp_path / "inbox"

    with pytest.raises(ValueError, match="rights"):
        await install_production_media(
            session,
            release_path=release_path,
            assets=assets,
            root=destination,
        )

    assert not (destination / str(run.raw_item_id)).exists()
    assert not [path for path in destination.iterdir() if path.name.startswith(".install-")]


@pytest.mark.asyncio
async def test_refuses_extra_assets_and_existing_bundle(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    package = _stored_package(run)
    caption = await _preview(session, run.raw_item_id)
    source = tmp_path / "source"
    _write_release(source, run, package, caption, asset_count=1)
    release_path, assets = _source_paths(source, run)
    destination = tmp_path / "inbox"

    (assets / "unreviewed.png").write_bytes(b"\x89PNG\r\n\x1a\nextra")
    with pytest.raises(ValueError, match="exactly match"):
        await install_production_media(
            session,
            release_path=release_path,
            assets=assets,
            root=destination,
        )
    (assets / "unreviewed.png").unlink()

    await install_production_media(
        session,
        release_path=release_path,
        assets=assets,
        root=destination,
    )
    with pytest.raises(ValueError, match="already exists"):
        await install_production_media(
            session,
            release_path=release_path,
            assets=assets,
            root=destination,
        )


@pytest.mark.asyncio
async def test_environment_guard_runs_before_input_io(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = reviewed
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValueError, match="APP_ENV=production"):
        await install_production_media(
            session,
            release_path=tmp_path / "missing-release.json",
            assets=tmp_path / "missing-assets",
            root=tmp_path / "inbox",
        )
