"""Provenance-bound local media; historical photos require explicit date labels."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: Literal["self_authored_card", "licensed_photo", "official_poster"]
    source_url: str = Field(min_length=8)
    rights_basis: str = Field(min_length=10)
    rights_verified: bool
    attribution: str = Field(min_length=1)
    archive_year: int | None = Field(default=None, ge=1900, le=2100)
    archive_label: str = ""


class MediaReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reviewer_id: str = Field(min_length=1)
    role: Literal["facts", "utility"]
    verdict: Literal["approve", "hold", "reject"]
    score: float = Field(ge=0, le=100)
    flags: tuple[str, ...]
    live_source_checked: bool
    checked_at: AwareDatetime
    reviewed_asset_sha256: tuple[str, ...]
    caption_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rationale: str = Field(min_length=1)


class MediaRelease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    raw_item_id: int = Field(gt=0)
    post_id: int = Field(gt=0)
    editorial_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: str = Field(pattern=r"^-100\d+$")
    caption: str = Field(min_length=1)
    assets: tuple[MediaAsset, ...] = Field(min_length=1, max_length=10)
    reviews: tuple[MediaReview, MediaReview]


def media_context_sha256(release: MediaRelease) -> str:
    value = json.dumps(release.model_dump(mode="json", exclude={"reviews"}),
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def validate_media(
    release: MediaRelease, directory: Path, *, now: datetime | None = None,
) -> tuple[Path, ...]:
    now = now or datetime.now(UTC)
    if now.utcoffset() is None or len(release.caption.encode("utf-16-le")) // 2 > 1024:
        raise ValueError("aware time and caption <=1024 UTF-16 units required")
    hashes = tuple(asset.sha256 for asset in release.assets)
    if len(set(hashes)) != len(hashes):
        raise ValueError("duplicate images")
    paths = []
    for asset in release.assets:
        if not asset.rights_verified:
            raise ValueError("media reuse rights not verified")
        if asset.origin == "licensed_photo" and asset.archive_year is None:
            raise ValueError("photo event year must be known")
        if asset.archive_year is not None and asset.archive_year > now.year:
            raise ValueError("photo event year is in the future")
        if not asset.file_name or any(c in asset.file_name for c in ("/", "\\", ":")):
            raise ValueError("media file name must be a basename")
        path = directory / asset.file_name
        if path.is_symlink() or not path.is_file():
            raise ValueError("local regular media file required")
        if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
            raise ValueError("media changed after review")
        if asset.archive_year is not None:
            if (str(asset.archive_year) not in asset.archive_label
                    or asset.archive_label not in release.caption):
                raise ValueError("archive year and exact label required in caption")
        if asset.origin in {"licensed_photo", "official_poster"} and (
            asset.attribution not in release.caption
        ):
            raise ValueError("photo attribution required in caption")
        paths.append(path)
    if {r.role for r in release.reviews} != {"facts", "utility"}:
        raise ValueError("two independent media review roles required")
    if len({r.reviewer_id.strip().casefold() for r in release.reviews}) != 2:
        raise ValueError("distinct media reviewers required")
    caption_hash = hashlib.sha256(release.caption.encode()).hexdigest()
    for review in release.reviews:
        if (review.verdict != "approve" or review.score < 75 or review.flags
                or not review.live_source_checked
                or not timedelta(0) <= now - review.checked_at <= timedelta(hours=24)
                or review.caption_sha256 != caption_hash
                or review.context_sha256 != media_context_sha256(release)
                or review.reviewed_asset_sha256 != hashes):
            raise ValueError("media review missing, expired, or mismatched")
    return tuple(paths)
