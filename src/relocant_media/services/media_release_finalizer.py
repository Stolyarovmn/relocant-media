"""Prepare immutable media review context and finalize only explicit independent approvals."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.media import (
    MediaAsset,
    MediaRelease,
    MediaReview,
    validate_media,
    validate_media_assets,
)
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.editorial import EditorialService
from relocant_media.services.production_media import (
    MAX_RELEASE_BYTES,
    expected_media_caption,
    validate_telegram_photo,
)
from relocant_media.storage.models import EditorialRun, Post


class ExplicitMediaAssets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    assets: tuple[MediaAsset, ...] = Field(min_length=1, max_length=10)


class ExplicitMediaReviews(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reviews: tuple[MediaReview, MediaReview]


class MediaReleaseDraft(BaseModel):
    """Exact MediaRelease review context before independent reviews are attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    raw_item_id: int = Field(gt=0)
    post_id: int = Field(gt=0)
    editorial_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: str = Field(pattern=r"^-100\d+$")
    caption: str = Field(min_length=1)
    assets: tuple[MediaAsset, ...] = Field(min_length=1, max_length=10)


class MediaReviewContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    caption_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset_sha256: tuple[str, ...]
    required_roles: tuple[str, str]
    publication_permitted: Literal[False]
    rights_inferred: Literal[False]
    telegram_io_performed: Literal[False]


def media_draft_context_sha256(draft: MediaReleaseDraft) -> str:
    value = json.dumps(
        draft.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _read_bounded_json(path: Path, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")
    data = path.read_bytes()
    if not data or len(data) > MAX_RELEASE_BYTES:
        raise ValueError(f"{label} size is invalid")
    return data


def _require_reviewable_source(asset: MediaAsset) -> None:
    try:
        parsed = urlsplit(asset.source_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("media source URL is invalid") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("media source URL must be credential-free HTTPS")


def _copy_review_assets(
    assets: tuple[MediaAsset, ...],
    source: Path,
    destination: Path,
) -> None:
    expected = {asset.file_name for asset in assets}
    observed = {entry.name for entry in source.iterdir()}
    if observed != expected:
        raise ValueError("media review assets must exactly match the manifest")
    destination.mkdir(parents=False, exist_ok=False)
    for asset in assets:
        origin = source / asset.file_name
        if origin.is_symlink() or not origin.is_file():
            raise ValueError("media review asset must be a regular file")
        data = origin.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("media review asset hash does not match manifest")
        validate_telegram_photo(origin, data)
        target = destination / asset.file_name
        with target.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


def _write_json(path: Path, value: BaseModel) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(value.model_dump_json(indent=2))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


async def prepare_media_release_review_bundle(
    session: Session,
    *,
    raw_item_id: int,
    release_id: str,
    assets_manifest: Path,
    assets: Path,
    output: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    """Freeze exact caption/assets for independent review; never infer rights or publish."""
    if os.environ.get("APP_ENV", "").lower() != "production":
        raise ValueError("APP_ENV=production required")
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("media review time must be timezone-aware")
    if assets.is_symlink() or not assets.is_dir():
        raise ValueError("media review assets must be a regular directory")

    explicit = ExplicitMediaAssets.model_validate_json(
        _read_bounded_json(assets_manifest, "media assets manifest")
    )
    for asset in explicit.assets:
        if not asset.rights_verified:
            raise ValueError("media reuse rights must be explicitly verified before review")
        _require_reviewable_source(asset)

    run = session.get(EditorialRun, raw_item_id)
    if run is None or run.post_id is None or "_package" not in run.outcome:
        raise ValueError("current reviewed editorial package is required")
    if run.delivery_status != "pending":
        raise ValueError("media review bundle can only be prepared before delivery")
    post = session.get(Post, run.post_id)
    if post is None or post.telegram_message_id is not None:
        raise ValueError("media review post is missing or already delivered")

    package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    if package.raw_item_id != raw_item_id:
        raise ValueError("current editorial package identity does not match")
    package_sha256 = hashlib.sha256(
        canonical_json(package.model_dump(mode="json")).encode()
    ).hexdigest()
    if run.outcome.get("_package_sha256") != package_sha256:
        raise ValueError("current editorial package fingerprint does not match")

    service = EditorialService(session, cast(EditorialPipeline, None))
    preview = await service.deliver(raw_item_id, dry_run=True)
    caption = expected_media_caption(explicit.assets, preview.text)
    validate_media_assets(explicit.assets, assets, caption, now=now)

    draft = MediaReleaseDraft(
        version=1,
        release_id=release_id,
        raw_item_id=raw_item_id,
        post_id=post.id,
        editorial_package_sha256=package_sha256,
        target_id=str(APPROVED_CHANNEL_ID),
        caption=caption,
        assets=explicit.assets,
    )
    context = MediaReviewContext(
        version=1,
        context_sha256=media_draft_context_sha256(draft),
        caption_sha256=hashlib.sha256(caption.encode()).hexdigest(),
        asset_sha256=tuple(asset.sha256 for asset in explicit.assets),
        required_roles=("facts", "utility"),
        publication_permitted=False,
        rights_inferred=False,
        telegram_io_performed=False,
    )

    if output.exists():
        raise ValueError("media review output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("media review output parent must be a regular directory")
    staging = output.parent / f".{output.name}.prepare-{uuid.uuid4().hex}"
    try:
        staging.mkdir(exist_ok=False)
        _copy_review_assets(explicit.assets, assets, staging / "assets")
        _write_json(staging / "release-draft.json", draft)
        _write_json(staging / "review-context.json", context)
        os.rename(staging, output)
        parent_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        session.rollback()

    return {
        "state": "awaiting_independent_media_reviews",
        "raw_item_id": raw_item_id,
        "post_id": post.id,
        "release_id": release_id,
        "context_sha256": context.context_sha256,
        "caption_sha256": context.caption_sha256,
        "asset_sha256": list(context.asset_sha256),
        "publication_permitted": False,
        "rights_inferred": False,
        "telegram_io_performed": False,
    }


def finalize_media_release(
    bundle: Path,
    *,
    reviews_path: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    """Attach exactly two explicit matching reviews and seal release.json without installation."""
    now = now or datetime.now(UTC)
    if now.utcoffset() is None:
        raise ValueError("media review time must be timezone-aware")
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("media review bundle must be a regular directory")
    release_path = bundle / "release.json"
    if release_path.exists():
        raise ValueError("media release is already finalized")
    assets = bundle / "assets"
    if assets.is_symlink() or not assets.is_dir():
        raise ValueError("media review bundle assets are missing")

    draft = MediaReleaseDraft.model_validate_json(
        _read_bounded_json(bundle / "release-draft.json", "media release draft")
    )
    context = MediaReviewContext.model_validate_json(
        _read_bounded_json(bundle / "review-context.json", "media review context")
    )
    reviews = ExplicitMediaReviews.model_validate_json(
        _read_bounded_json(reviews_path, "media reviews")
    )

    expected_context = media_draft_context_sha256(draft)
    expected_caption = hashlib.sha256(draft.caption.encode()).hexdigest()
    expected_assets = tuple(asset.sha256 for asset in draft.assets)
    if (
        context.context_sha256 != expected_context
        or context.caption_sha256 != expected_caption
        or context.asset_sha256 != expected_assets
        or context.required_roles != ("facts", "utility")
        or context.publication_permitted is not False
        or context.rights_inferred is not False
        or context.telegram_io_performed is not False
    ):
        raise ValueError("media review context does not match frozen draft")

    release = MediaRelease.model_validate(
        {**draft.model_dump(mode="python"), "reviews": reviews.reviews}
    )
    validate_media(release, assets, now=now)

    data = release.model_dump_json(indent=2).encode() + b"\n"
    temporary = bundle / f".release-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, release_path)
        temporary.unlink()
        bundle_fd = os.open(bundle, os.O_RDONLY)
        try:
            os.fsync(bundle_fd)
        finally:
            os.close(bundle_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "state": "finalized",
        "raw_item_id": release.raw_item_id,
        "post_id": release.post_id,
        "release_id": release.release_id,
        "context_sha256": expected_context,
        "publication_permitted": True,
        "production_install_performed": False,
        "telegram_io_performed": False,
    }
