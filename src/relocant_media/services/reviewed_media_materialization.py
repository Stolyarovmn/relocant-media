"""Materialize explicitly reviewed cover assessments into provenance-bound local assets."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy.orm import Session

from relocant_media.bot.media import MediaAsset
from relocant_media.collectors.article_media import (
    ArticleMediaCandidate,
    extract_article_media_candidates,
    download_article_media,
)
from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.media_review_packet import (
    build_media_review_packet,
    plan_reviewed_packet,
)
from relocant_media.services.production_media import validate_telegram_photo
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import RawItem, Source

AssetOrigin = Literal["official_photo", "licensed_photo", "official_poster"]


def _require_staging() -> None:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")


def _require_https_url(value: str, label: str) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} URL is invalid") from error
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError(f"{label} must be credential-free HTTPS")


def _approved_image_hosts(snapshot: RawItemMediaSnapshot) -> frozenset[str]:
    value = snapshot.manifest.get("approved_image_hosts", [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("saved approved image hosts are invalid")
    return frozenset(value)


def _reextract_candidate(
    snapshot: RawItemMediaSnapshot,
    assessment: CoverAssessment,
    approved_hosts: frozenset[str],
) -> ArticleMediaCandidate:
    if snapshot.source_html is None:
        raise ValueError("saved source HTML is required for media materialization")
    encoded = snapshot.source_html.encode("utf-8")
    if (
        len(encoded) != snapshot.source_html_bytes
        or sha256(encoded).hexdigest() != snapshot.source_html_sha256
    ):
        raise ValueError("saved source HTML fingerprint does not match")
    candidates = extract_article_media_candidates(
        snapshot.source_html,
        snapshot.source_page,
        approved_image_hosts=approved_hosts,
    )
    matches = [
        candidate
        for candidate in candidates
        if candidate.url == assessment.image_url and candidate.source_page == assessment.source_page
    ]
    if len(matches) != 1:
        raise ValueError("reviewed media candidate is not reproducible from saved source HTML")
    return matches[0]


def _packet_candidate(
    packet: dict[str, object], assessment: CoverAssessment
) -> tuple[int, str]:
    values = packet.get("candidates")
    if not isinstance(values, list):
        raise ValueError("media review packet candidates are invalid")
    matches: list[tuple[int, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        candidate = value.get("candidate")
        source_raw_item_id = value.get("source_raw_item_id")
        source_html_sha256 = value.get("source_html_sha256")
        if (
            not isinstance(candidate, dict)
            or not isinstance(source_raw_item_id, int)
            or not isinstance(source_html_sha256, str)
        ):
            continue
        if (
            candidate.get("url") == assessment.image_url
            and candidate.get("source_page") == assessment.source_page
        ):
            matches.append((source_raw_item_id, source_html_sha256))
    if len(matches) != 1:
        raise ValueError("reviewed media assessment is not uniquely bound to the review packet")
    return matches[0]


def _source_for_snapshot(
    session: Session, snapshot: RawItemMediaSnapshot
) -> tuple[RawItem, Source]:
    raw = session.get(RawItem, snapshot.raw_item_id)
    if raw is None:
        raise ValueError("media source raw item is missing")
    if snapshot.source_page not in {raw.url, raw.canonical_url}:
        raise ValueError("media snapshot page no longer matches its raw item")
    source = session.get(Source, raw.source_id)
    if source is None:
        raise ValueError("media source metadata is missing")
    return raw, source


def _origin_for_assessment(assessment: CoverAssessment, source: Source) -> AssetOrigin:
    if assessment.kind == "official_poster":
        return "official_poster"
    if assessment.kind == "archive_photo":
        return "licensed_photo"
    if assessment.kind == "current_photo" and source.ownership == "official":
        return "official_photo"
    raise ValueError("current photo materialization requires explicitly official source ownership")


def _require_reviewed_assessment(assessment: CoverAssessment) -> None:
    if (
        not assessment.event_match
        or assessment.rights_status != "reviewed"
        or assessment.rights_method == "unknown"
        or not assessment.rights_evidence_url
        or not assessment.rights_rationale
        or not assessment.attribution
    ):
        raise ValueError("explicit completed rights and event review is required")
    _require_https_url(assessment.source_page, "media source page")
    _require_https_url(assessment.rights_evidence_url, "rights evidence")


def _rights_basis(assessment: CoverAssessment) -> str:
    return (
        f"Reviewed by {assessment.assessed_by}; method={assessment.rights_method}; "
        f"evidence={assessment.rights_evidence_url}; rationale={assessment.rights_rationale}"
    )


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def materialize_reviewed_media_assets(
    session: Session,
    *,
    raw_item_id: int,
    assessments: tuple[CoverAssessment, ...],
    event_year: int,
    output: Path,
    client: httpx.Client,
    allow_album: bool = False,
) -> dict[str, object]:
    """Download only already-reviewed selected candidates and emit an exact asset manifest."""
    _require_staging()
    if output.exists():
        raise ValueError("new media materialization output directory required")
    if client.follow_redirects:
        raise ValueError("automatic HTTP redirects must be disabled")

    packet = build_media_review_packet(session, raw_item_id)
    plan = plan_reviewed_packet(
        packet,
        assessments,
        event_year=event_year,
        allow_album=allow_album,
    )
    selected_value = plan.get("selected")
    if not isinstance(selected_value, list):
        raise ValueError("cover plan selected assets are invalid")
    selected = tuple(CoverAssessment.model_validate(value) for value in selected_value)
    if not selected:
        return {
            "version": 1,
            "raw_item_id": raw_item_id,
            "mode": "text",
            "reason": plan.get("reason", "no_suitable_cover_publish_text"),
            "asset_count": 0,
            "output_created": False,
            "download_performed": False,
            "rights_inferred": False,
            "publication_permitted": False,
            "telegram_io_performed": False,
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ValueError("media materialization output parent must be a regular directory")
    staging = output.parent / f".{output.name}.materialize-{uuid.uuid4().hex}"
    audit_items: list[dict[str, object]] = []
    assets: list[MediaAsset] = []
    seen_hashes: set[str] = set()
    try:
        staging.mkdir(exist_ok=False)
        assets_dir = staging / "assets"
        assets_dir.mkdir(exist_ok=False)
        for index, assessment in enumerate(selected):
            _require_reviewed_assessment(assessment)
            source_raw_item_id, expected_html_sha256 = _packet_candidate(packet, assessment)
            snapshot = session.get(RawItemMediaSnapshot, source_raw_item_id)
            if snapshot is None or snapshot.source_html_sha256 != expected_html_sha256:
                raise ValueError("media snapshot no longer matches the review packet")
            _, source = _source_for_snapshot(session, snapshot)
            origin = _origin_for_assessment(assessment, source)
            approved_hosts = _approved_image_hosts(snapshot)
            candidate = _reextract_candidate(snapshot, assessment, approved_hosts)

            download_dir = staging / f".download-{index}"
            downloaded = download_article_media(
                candidate,
                download_dir,
                client,
                approved_image_hosts=approved_hosts,
            )
            data = downloaded.path.read_bytes()
            validate_telegram_photo(downloaded.path, data)
            if downloaded.sha256 in seen_hashes:
                raise ValueError("reviewed media resolved to duplicate image bytes")
            seen_hashes.add(downloaded.sha256)
            final_path = assets_dir / downloaded.path.name
            if final_path.exists():
                raise ValueError("materialized media asset file collision")
            os.rename(downloaded.path, final_path)
            download_dir.rmdir()

            archive_year = assessment.event_year if assessment.kind == "archive_photo" else None
            archive_label = assessment.archive_label if assessment.kind == "archive_photo" else ""
            asset = MediaAsset(
                file_name=final_path.name,
                sha256=downloaded.sha256,
                origin=origin,
                source_url=assessment.source_page,
                rights_basis=_rights_basis(assessment),
                rights_verified=True,
                attribution=assessment.attribution,
                archive_year=archive_year,
                archive_label=archive_label,
            )
            assets.append(asset)
            audit_items.append(
                {
                    "source_raw_item_id": source_raw_item_id,
                    "source_id": source.id,
                    "source_ownership": source.ownership,
                    "source_page": assessment.source_page,
                    "source_html_sha256": snapshot.source_html_sha256,
                    "approved_image_hosts": sorted(approved_hosts),
                    "reviewed_image_url": assessment.image_url,
                    "downloaded_final_url": downloaded.source_url,
                    "asset_sha256": downloaded.sha256,
                    "assessed_by": assessment.assessed_by,
                    "rights_method": assessment.rights_method,
                    "rights_evidence_url": assessment.rights_evidence_url,
                }
            )

        manifest = {"assets": [asset.model_dump(mode="json") for asset in assets]}
        audit = {
            "version": 1,
            "raw_item_id": raw_item_id,
            "post_id": packet.get("post_id"),
            "editorial_package_sha256": packet.get("editorial_package_sha256"),
            "mode": plan.get("mode"),
            "asset_count": len(assets),
            "items": audit_items,
            "rights_inferred": False,
            "publication_permitted": False,
            "independent_media_reviews_completed": False,
            "telegram_io_performed": False,
        }
        _write_json(staging / "assets.json", manifest)
        _write_json(staging / "materialization.json", audit)
        os.rename(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "version": 1,
        "raw_item_id": raw_item_id,
        "mode": plan.get("mode"),
        "asset_count": len(assets),
        "output_created": True,
        "download_performed": True,
        "rights_inferred": False,
        "publication_permitted": False,
        "requires_independent_media_reviews": True,
        "telegram_io_performed": False,
    }
