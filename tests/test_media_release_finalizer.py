import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_production_media_delivery import _preview
from test_production_media_delivery import reviewed as _reviewed

from relocant_media.bot.media import MediaRelease
from relocant_media.services.media_release_finalizer import (
    MediaReleaseDraft,
    MediaReviewContext,
    finalize_media_release,
    prepare_media_release_review_bundle,
)
from relocant_media.services.production_media import prepare_production_media

reviewed = _reviewed


def _write_asset_manifest(
    root: Path,
    *,
    rights_verified: bool = True,
    extra_asset: bool = False,
) -> tuple[Path, Path]:
    assets = root / "source-assets"
    assets.mkdir(parents=True)
    photo = assets / "official.jpg"
    photo.write_bytes(b"\xff\xd8\xffreviewed-official-photo")
    digest = hashlib.sha256(photo.read_bytes()).hexdigest()
    if extra_asset:
        (assets / "unreviewed.png").write_bytes(b"\x89PNG\r\n\x1a\nextra")
    manifest = root / "assets.json"
    manifest.write_text(
        json.dumps(
            {
                "assets": [
                    {
                        "file_name": photo.name,
                        "sha256": digest,
                        "origin": "official_photo",
                        "source_url": "https://www.beograd.rs/vesti/example",
                        "rights_basis": "Explicit reuse permission recorded by the reviewer",
                        "rights_verified": rights_verified,
                        "attribution": "Фото: Град Београд",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return manifest, assets


def _write_reviews(bundle: Path, *, context_override: str | None = None) -> Path:
    draft = MediaReleaseDraft.model_validate_json((bundle / "release-draft.json").read_bytes())
    context = MediaReviewContext.model_validate_json((bundle / "review-context.json").read_bytes())
    checked_at = datetime.now(UTC).isoformat()
    reviews = bundle.parent / f"reviews-{draft.raw_item_id}.json"
    reviews.write_text(
        json.dumps(
            {
                "reviews": [
                    {
                        "reviewer_id": f"independent-{role}",
                        "role": role,
                        "verdict": "approve",
                        "score": 90,
                        "flags": [],
                        "live_source_checked": True,
                        "checked_at": checked_at,
                        "reviewed_asset_sha256": list(context.asset_sha256),
                        "caption_sha256": context.caption_sha256,
                        "context_sha256": context_override or context.context_sha256,
                        "rationale": "Checked exact photo, attribution, rights and frozen caption",
                    }
                    for role in ("facts", "utility")
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return reviews


@pytest.mark.asyncio
async def test_finalize_official_photo_release_then_production_loader_accepts_it(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    manifest, assets = _write_asset_manifest(tmp_path)
    root = tmp_path / "review-bundles"
    bundle = root / str(run.raw_item_id)

    prepared = await prepare_media_release_review_bundle(
        session,
        raw_item_id=run.raw_item_id,
        release_id=f"media-{run.raw_item_id}",
        assets_manifest=manifest,
        assets=assets,
        output=bundle,
    )

    assert prepared["publication_permitted"] is False
    assert prepared["rights_inferred"] is False
    assert prepared["telegram_io_performed"] is False
    draft = MediaReleaseDraft.model_validate_json((bundle / "release-draft.json").read_bytes())
    assert draft.assets[0].origin == "official_photo"
    assert draft.assets[0].archive_year is None
    assert "Фото: Град Београд" in draft.caption

    result = finalize_media_release(bundle, reviews_path=_write_reviews(bundle))
    assert result["publication_permitted"] is True
    assert result["production_install_performed"] is False
    assert result["telegram_io_performed"] is False

    release = MediaRelease.model_validate_json((bundle / "release.json").read_bytes())
    editorial_text = await _preview(session, run.raw_item_id)
    loaded = prepare_production_media(
        raw_item_id=run.raw_item_id,
        post_id=run.post_id,
        editorial_package_sha256=release.editorial_package_sha256,
        target_channel_id=int(release.target_id),
        editorial_text=editorial_text,
        root=root,
    )
    assert loaded is not None
    assert len(loaded.uploads) == 1
    assert loaded.caption == draft.caption


@pytest.mark.asyncio
async def test_prepare_never_infers_missing_rights(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    manifest, assets = _write_asset_manifest(tmp_path, rights_verified=False)
    bundle = tmp_path / "review-bundles" / str(run.raw_item_id)

    with pytest.raises(ValueError, match="explicitly verified"):
        await prepare_media_release_review_bundle(
            session,
            raw_item_id=run.raw_item_id,
            release_id=f"media-{run.raw_item_id}",
            assets_manifest=manifest,
            assets=assets,
            output=bundle,
        )

    assert not bundle.exists()


@pytest.mark.asyncio
async def test_prepare_rejects_unreviewed_extra_asset(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    manifest, assets = _write_asset_manifest(tmp_path, extra_asset=True)
    bundle = tmp_path / "review-bundles" / str(run.raw_item_id)

    with pytest.raises(ValueError, match="exactly match"):
        await prepare_media_release_review_bundle(
            session,
            raw_item_id=run.raw_item_id,
            release_id=f"media-{run.raw_item_id}",
            assets_manifest=manifest,
            assets=assets,
            output=bundle,
        )

    assert not bundle.exists()


@pytest.mark.asyncio
async def test_finalize_rejects_mismatched_review_without_release(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    manifest, assets = _write_asset_manifest(tmp_path)
    bundle = tmp_path / "review-bundles" / str(run.raw_item_id)
    await prepare_media_release_review_bundle(
        session,
        raw_item_id=run.raw_item_id,
        release_id=f"media-{run.raw_item_id}",
        assets_manifest=manifest,
        assets=assets,
        output=bundle,
    )

    reviews = _write_reviews(bundle, context_override="f" * 64)
    with pytest.raises(ValueError, match="review missing, expired, or mismatched"):
        finalize_media_release(bundle, reviews_path=reviews)
    assert not (bundle / "release.json").exists()


@pytest.mark.asyncio
async def test_finalize_rejects_asset_changed_after_review_context(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    manifest, assets = _write_asset_manifest(tmp_path)
    bundle = tmp_path / "review-bundles" / str(run.raw_item_id)
    await prepare_media_release_review_bundle(
        session,
        raw_item_id=run.raw_item_id,
        release_id=f"media-{run.raw_item_id}",
        assets_manifest=manifest,
        assets=assets,
        output=bundle,
    )
    reviews = _write_reviews(bundle)
    (bundle / "assets" / "official.jpg").write_bytes(b"\xff\xd8\xfftampered")

    with pytest.raises(ValueError, match="changed after review"):
        finalize_media_release(bundle, reviews_path=reviews)
    assert not (bundle / "release.json").exists()
