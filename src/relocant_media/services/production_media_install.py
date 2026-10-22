"""Atomically install a fully reviewed media release for normal production delivery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import cast

from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.media import MediaRelease
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.editorial import EditorialService
from relocant_media.services.production_media import (
    MAX_RELEASE_BYTES,
    prepare_production_media,
    production_media_root,
)
from relocant_media.storage.models import EditorialRun, Post


def _read_release(path: Path) -> tuple[bytes, MediaRelease]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("reviewed media release must be a regular file")
    data = path.read_bytes()
    if not data or len(data) > MAX_RELEASE_BYTES:
        raise ValueError("reviewed media release size is invalid")
    return data, MediaRelease.model_validate_json(data)


def _copy_exact_assets(release: MediaRelease, source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("reviewed media assets must be a regular directory")
    expected = {asset.file_name for asset in release.assets}
    observed = {entry.name for entry in source.iterdir()}
    if observed != expected:
        raise ValueError("reviewed media assets must exactly match the release")
    destination.mkdir(parents=False, exist_ok=False)
    for asset in release.assets:
        origin = source / asset.file_name
        if origin.is_symlink() or not origin.is_file():
            raise ValueError("reviewed media asset must be a regular file")
        data = origin.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("reviewed media asset hash does not match release")
        target = destination / asset.file_name
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


async def install_production_media(
    session: Session,
    *,
    release_path: Path,
    assets: Path,
    root: Path | None = None,
) -> dict[str, object]:
    """Validate against current editorial state, then atomically install without Telegram I/O."""
    if os.environ.get("APP_ENV", "").lower() != "production":
        raise ValueError("APP_ENV=production required")

    release_bytes, release = _read_release(release_path)
    if release.target_id != str(APPROVED_CHANNEL_ID):
        raise ValueError("reviewed media release target is not the approved production channel")

    run = session.get(EditorialRun, release.raw_item_id)
    if run is None or run.post_id != release.post_id or "_package" not in run.outcome:
        raise ValueError("current reviewed editorial package is required")
    if run.delivery_status != "pending":
        raise ValueError("production media can only be installed before delivery")
    post = session.get(Post, release.post_id)
    if post is None or post.telegram_message_id is not None:
        raise ValueError("production media post is missing or already delivered")

    package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    package_sha256 = hashlib.sha256(
        canonical_json(package.model_dump(mode="json")).encode()
    ).hexdigest()
    if run.outcome.get("_package_sha256") != package_sha256:
        raise ValueError("current editorial package fingerprint does not match")

    service = EditorialService(session, cast(EditorialPipeline, None))
    preview = await service.deliver(release.raw_item_id, dry_run=True)

    destination_root = production_media_root(root)
    if destination_root.exists() and destination_root.is_symlink():
        raise ValueError("production media root must not be a symlink")
    destination_root.mkdir(parents=True, exist_ok=True)
    final = destination_root / str(release.raw_item_id)
    if final.exists():
        raise ValueError("production media bundle already exists")

    staging_root = destination_root / f".install-{release.raw_item_id}-{uuid.uuid4().hex}"
    staged_bundle = staging_root / str(release.raw_item_id)
    try:
        staged_bundle.mkdir(parents=True, exist_ok=False)
        staged_release = staged_bundle / "release.json"
        with staged_release.open("xb") as handle:
            handle.write(release_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        _copy_exact_assets(release, assets, staged_bundle / "assets")

        prepared = prepare_production_media(
            raw_item_id=release.raw_item_id,
            post_id=release.post_id,
            editorial_package_sha256=package_sha256,
            target_channel_id=APPROVED_CHANNEL_ID,
            editorial_text=preview.text,
            root=staging_root,
        )
        if prepared is None:
            raise ValueError("staged production media bundle disappeared")
        os.replace(staged_bundle, final)
        directory_fd = os.open(destination_root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        staging_root.rmdir()
        return {
            "state": "installed",
            "raw_item_id": release.raw_item_id,
            "post_id": release.post_id,
            "release_id": release.release_id,
            "release_sha256": prepared.release_sha256,
            "asset_count": len(prepared.uploads),
            "target_id": release.target_id,
            "telegram_io_performed": False,
        }
    except BaseException:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
