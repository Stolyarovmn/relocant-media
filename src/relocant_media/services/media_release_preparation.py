"""Prepare reviewed media assets and seal them only after two explicit media reviews."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.media import MediaAsset, MediaRelease, MediaReview, validate_media
from relocant_media.collectors.article_media import (
    ArticleMediaCandidate,
    download_article_media,
)
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.editorial import EditorialService
from relocant_media.services.media_review_packet import (
    build_media_review_packet,
    plan_reviewed_packet,
)
from relocant_media.services.production_media import expected_media_caption
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import EditorialRun, Post

MAX_DRAFT_BYTES = 512 * 1024


class MediaReleaseDraft(BaseModel):
    """A release with immutable assets/caption but no media-review verdicts yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    raw_item_id: int = Field(gt=0)
    post_id: int = Field(gt=0)
    editorial_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_id: str = Field(pattern=r"^-100\d+$")
    caption: str = Field(min_length=1)
    assets: tuple[MediaAsset, ...] = Field(min_length=1, max_length=10)


def draft_context_sha256(draft: MediaReleaseDraft) -> str:
    value = json.dumps(
        draft.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _review_template(draft: MediaReleaseDraft) -> list[dict[str, object]]:
    asset_hashes = [asset.sha256 for asset in draft.assets]
    caption_hash = hashlib.sha256(draft.caption.encode()).hexdigest()
    context_hash = draft_context_sha256(draft)
    return [
        {
            "reviewer_id": "",
            "role": role,
            "verdict": "hold",
            "score": 0,
            "flags": ["review_required"],
            "live_source_checked": False,
            "checked_at": None,
            "reviewed_asset_sha256": asset_hashes,
            "caption_sha256": caption_hash,
            "context_sha256": context_hash,
            "rationale": "",
        }
        for role in ("facts", "utility")
    ]


def _rights_basis(assessment: CoverAssessment) -> str:
    return (
        f"{assessment.rights_method}: {assessment.rights_rationale} "
        f"Evidence: {assessment.rights_evidence_url}"
    )


def _asset_from_assessment(
    assessment: CoverAssessment,
    *,
    file_name: str,
    sha256: str,
    source_url: str,
) -> MediaAsset:
    if assessment.rights_status != "reviewed" or assessment.rights_method == "unknown":
        raise ValueError("selected media rights were not explicitly reviewed")
    if not assessment.rights_evidence_url or not assessment.rights_rationale:
        raise ValueError("selected media rights evidence is incomplete")
    if not assessment.attribution:
        raise ValueError("selected media attribution is required")

    origin: Literal["licensed_photo", "official_poster"]
    archive_year: int | None = None
    archive_label = ""
    if assessment.kind == "official_poster":
        origin = "official_poster"
    elif assessment.kind in {"current_photo", "archive_photo"}:
        if assessment.event_year is None:
            raise ValueError("reviewed photo year is required")
        origin = "licensed_photo"
        archive_year = assessment.event_year
        archive_label = assessment.archive_label.strip() or f"Фото {assessment.event_year} года"
    else:
        raise ValueError("unsupported selected media kind")

    return MediaAsset(
        file_name=file_name,
        sha256=sha256,
        origin=origin,
        source_url=source_url,
        rights_basis=_rights_basis(assessment),
        rights_verified=True,
        attribution=assessment.attribution,
        archive_year=archive_year,
        archive_label=archive_label,
    )


def _bound_candidate(
    session: Session,
    packet: dict[str, Any],
    assessment: CoverAssessment,
) -> tuple[ArticleMediaCandidate, frozenset[str]]:
    for entry in packet.get("candidates", []):
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("candidate")
        if not isinstance(candidate, dict):
            continue
        if (
            candidate.get("url") != assessment.image_url
            or candidate.get("source_page") != assessment.source_page
        ):
            continue
        source_raw_item_id = entry.get("source_raw_item_id")
        if not isinstance(source_raw_item_id, int):
            raise ValueError("media candidate provenance is incomplete")
        snapshot = session.get(RawItemMediaSnapshot, source_raw_item_id)
        if snapshot is None:
            raise ValueError("media candidate snapshot disappeared")
        stored = snapshot.manifest.get("candidates", [])
        if candidate not in stored:
            raise ValueError("media candidate no longer matches immutable provenance")
        hosts = snapshot.manifest.get("approved_image_hosts", [])
        if not isinstance(hosts, list) or any(not isinstance(host, str) for host in hosts):
            raise ValueError("approved image hosts provenance is invalid")
        return ArticleMediaCandidate(**candidate), frozenset(hosts)
    raise ValueError("selected media is not bound to the current review packet")


async def prepare_media_release_bundle(
    session: Session,
    *,
    raw_item_id: int,
    assessments: tuple[CoverAssessment, ...],
    event_year: int,
    output: Path,
    allow_album: bool = False,
    client: httpx.Client | None = None,
) -> dict[str, object]:
    """Download only explicitly approved candidates and create review-bound release material."""
    if output.exists():
        raise ValueError("new output directory required")
    packet = build_media_review_packet(session, raw_item_id)
    plan = plan_reviewed_packet(
        packet,
        assessments,
        event_year=event_year,
        allow_album=allow_album,
    )
    selected_values = plan.get("selected", [])
    if not isinstance(selected_values, list) or not selected_values:
        return {
            "mode": "text",
            "raw_item_id": raw_item_id,
            "reason": plan.get("reason", "no_suitable_cover_publish_text"),
            "assets_downloaded": 0,
            "publication_permitted": False,
            "telegram_io_performed": False,
        }

    run = session.get(EditorialRun, raw_item_id)
    if (
        run is None
        or run.outcome.get("status") != "ready"
        or run.post_id is None
        or run.delivery_status != "pending"
    ):
        raise ValueError("current READY editorial run is required")
    post = session.get(Post, run.post_id)
    if post is None or post.telegram_message_id is not None:
        raise ValueError("current undelivered editorial post is required")
    package_sha256 = packet.get("editorial_package_sha256")
    if not isinstance(package_sha256, str) or run.outcome.get("_package_sha256") != package_sha256:
        raise ValueError("reviewed editorial package fingerprint changed")

    service = EditorialService(session, cast(EditorialPipeline, None))
    preview = await service.deliver(raw_item_id, dry_run=True)
    selected = tuple(CoverAssessment.model_validate(value) for value in selected_values)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".media-release-", dir=output.parent))
    owns_client = client is None
    active_client = client or httpx.Client(
        timeout=30,
        trust_env=False,
        follow_redirects=False,
    )
    try:
        assets_dir = temporary / "assets"
        assets_dir.mkdir()
        assets: list[MediaAsset] = []
        seen_hashes: set[str] = set()
        for index, assessment in enumerate(selected, start=1):
            candidate, approved_hosts = _bound_candidate(session, packet, assessment)
            downloaded = download_article_media(
                candidate,
                temporary / f"download-{index}",
                active_client,
                approved_image_hosts=approved_hosts,
            )
            if downloaded.sha256 in seen_hashes:
                raise ValueError("selected media resolved to duplicate bytes")
            seen_hashes.add(downloaded.sha256)
            destination = assets_dir / downloaded.path.name
            downloaded.path.replace(destination)
            downloaded.path.parent.rmdir()
            assets.append(
                _asset_from_assessment(
                    assessment,
                    file_name=destination.name,
                    sha256=downloaded.sha256,
                    source_url=downloaded.source_url,
                )
            )

        asset_tuple = tuple(assets)
        caption = expected_media_caption(asset_tuple, preview.text)
        asset_fingerprint = hashlib.sha256(
            "".join(asset.sha256 for asset in asset_tuple).encode()
        ).hexdigest()
        draft = MediaReleaseDraft(
            version=1,
            release_id=(
                f"media-{raw_item_id}-{package_sha256[:10]}-{asset_fingerprint[:10]}"
            ),
            raw_item_id=raw_item_id,
            post_id=run.post_id,
            editorial_package_sha256=package_sha256,
            target_id=str(APPROVED_CHANNEL_ID),
            caption=caption,
            assets=asset_tuple,
        )
        (temporary / "release-draft.json").write_text(
            draft.model_dump_json(indent=2), encoding="utf-8"
        )
        (temporary / "media-review-template.json").write_text(
            json.dumps(_review_template(draft), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (temporary / "preparation.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "raw_item_id": raw_item_id,
                    "post_id": run.post_id,
                    "editorial_package_sha256": package_sha256,
                    "target_id": str(APPROVED_CHANNEL_ID),
                    "plan": plan,
                    "selected_assessments": [item.model_dump(mode="json") for item in selected],
                    "asset_sha256": [asset.sha256 for asset in asset_tuple],
                    "requires_independent_media_reviews": True,
                    "publication_permitted": False,
                    "telegram_io_performed": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, output)
        return {
            "mode": plan["mode"],
            "raw_item_id": raw_item_id,
            "post_id": run.post_id,
            "release_id": draft.release_id,
            "assets_downloaded": len(asset_tuple),
            "review_required": True,
            "publication_permitted": False,
            "telegram_io_performed": False,
        }
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    finally:
        if owns_client:
            active_client.close()


def seal_media_release_bundle(
    *,
    bundle: Path,
    reviews_path: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    """Seal a prepared bundle only when two complete independent reviews validate exactly."""
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("prepared media bundle must be a regular directory")
    draft_path = bundle / "release-draft.json"
    assets_dir = bundle / "assets"
    release_path = bundle / "release.json"
    if release_path.exists():
        raise ValueError("media release is already sealed")
    if draft_path.is_symlink() or not draft_path.is_file():
        raise ValueError("media release draft is missing")
    if assets_dir.is_symlink() or not assets_dir.is_dir():
        raise ValueError("prepared media assets are missing")
    if reviews_path.is_symlink() or not reviews_path.is_file():
        raise ValueError("media reviews must be a regular file")

    draft_bytes = draft_path.read_bytes()
    if not draft_bytes or len(draft_bytes) > MAX_DRAFT_BYTES:
        raise ValueError("media release draft size is invalid")
    draft = MediaReleaseDraft.model_validate_json(draft_bytes)
    values = json.loads(reviews_path.read_text(encoding="utf-8"))
    if not isinstance(values, list) or len(values) != 2:
        raise ValueError("exactly two media reviews are required")
    reviews = tuple(MediaReview.model_validate(value) for value in values)
    release = MediaRelease.model_validate(
        {**draft.model_dump(mode="python"), "reviews": reviews}
    )

    expected_files = {asset.file_name for asset in release.assets}
    observed_files = {entry.name for entry in assets_dir.iterdir()}
    if observed_files != expected_files:
        raise ValueError("prepared assets no longer exactly match the draft")
    validate_media(release, assets_dir, now=now or datetime.now(UTC))
    encoded = release.model_dump_json(indent=2).encode()
    with release_path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "state": "sealed",
        "raw_item_id": release.raw_item_id,
        "post_id": release.post_id,
        "release_id": release.release_id,
        "asset_count": len(release.assets),
        "release_sha256": hashlib.sha256(encoded).hexdigest(),
        "publication_permitted": False,
        "telegram_io_performed": False,
    }
