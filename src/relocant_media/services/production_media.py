"""Load an explicitly reviewed media bundle for the normal production delivery path."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from relocant_media.ai.package import canonical_json
from relocant_media.bot.api import PhotoUpload
from relocant_media.bot.media import MediaAsset, MediaRelease, validate_media

DEFAULT_PRODUCTION_MEDIA_ROOT = Path("/var/lib/relocant-media/media-releases")
MAX_RELEASE_BYTES = 512 * 1024
MAX_ASSET_BYTES = 10 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"


@dataclass(frozen=True)
class PreparedProductionMedia:
    release: MediaRelease
    caption: str
    uploads: tuple[PhotoUpload, ...]
    release_sha256: str


def production_media_root(root: Path | None = None) -> Path:
    if root is not None:
        return root
    configured = os.environ.get("EDITORIAL_MEDIA_RELEASE_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_PRODUCTION_MEDIA_ROOT


def expected_media_caption(assets: tuple[MediaAsset, ...], editorial_text: str) -> str:
    """Build the exact caption used by reviewed production media delivery."""
    credits = list(
        dict.fromkeys(
            value
            for asset in assets
            if asset.origin in {"licensed_photo", "official_poster"}
            for value in (asset.archive_label, asset.attribution)
            if value
        )
    )
    return editorial_text + ("\n\n" + "\n".join(credits) if credits else "")


def _expected_caption(release: MediaRelease, editorial_text: str) -> str:
    return expected_media_caption(release.assets, editorial_text)


def _validate_telegram_photo(path: Path, data: bytes) -> None:
    if not data or len(data) > MAX_ASSET_BYTES:
        raise ValueError("production media asset size is invalid")
    suffix = path.suffix.lower()
    valid = (
        suffix == ".png" and data.startswith(PNG_SIGNATURE)
    ) or (
        suffix in {".jpg", ".jpeg"} and data.startswith(JPEG_SIGNATURE)
    )
    if not valid:
        raise ValueError("production media asset must be matching PNG or JPEG bytes")


def prepare_production_media(
    *,
    raw_item_id: int,
    post_id: int,
    editorial_package_sha256: str,
    target_channel_id: int,
    editorial_text: str,
    root: Path | None = None,
    now: datetime | None = None,
) -> PreparedProductionMedia | None:
    """Return reviewed immutable uploads, or None when no media bundle was supplied.

    An absent bundle intentionally means text-only publication. Once a bundle directory exists,
    every mismatch fails closed so a tampered or stale media review cannot silently become a
    different publication.
    """
    media_root = production_media_root(root)
    bundle = media_root / str(raw_item_id)
    if not bundle.exists():
        return None
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("production media bundle must be a regular directory")

    release_path = bundle / "release.json"
    assets = bundle / "assets"
    if (
        not release_path.is_file()
        or release_path.is_symlink()
        or not assets.is_dir()
        or assets.is_symlink()
    ):
        raise ValueError("production media bundle is incomplete")
    release_bytes = release_path.read_bytes()
    if not release_bytes or len(release_bytes) > MAX_RELEASE_BYTES:
        raise ValueError("production media release size is invalid")

    release = MediaRelease.model_validate_json(release_bytes)
    if (
        release.raw_item_id != raw_item_id
        or release.post_id != post_id
        or release.editorial_package_sha256 != editorial_package_sha256
        or release.target_id != str(target_channel_id)
    ):
        raise ValueError("production media release identity does not match delivery")

    expected_caption = expected_media_caption(release.assets, editorial_text)
    if release.caption != expected_caption:
        raise ValueError("production media caption does not match reviewed editorial text")

    paths = validate_media(release, assets, now=now)
    uploads: list[PhotoUpload] = []
    for path, asset in zip(paths, release.assets, strict=True):
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("media changed while preparing production upload")
        _validate_telegram_photo(path, data)
        uploads.append(PhotoUpload(path.name, data))

    release_sha256 = hashlib.sha256(
        canonical_json(release.model_dump(mode="json")).encode()
    ).hexdigest()
    return PreparedProductionMedia(
        release=release,
        caption=release.caption,
        uploads=tuple(uploads),
        release_sha256=release_sha256,
    )
