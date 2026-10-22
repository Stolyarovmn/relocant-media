"""Bind ingested media candidates to a reviewed editorial package without granting rights."""

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.services.cover_selection import CoverAssessment, plan_cover
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import EditorialRun


def _text_mode(raw_item_id: int, *, reason: str) -> dict[str, Any]:
    return {
        "version": 1,
        "raw_item_id": raw_item_id,
        "mode": "text",
        "reason": reason,
        "candidates": [],
        "requires_independent_media_reviews": False,
        "download_permitted": False,
        "publication_permitted": False,
        "generated_media_used": False,
    }


def _document_ids(run: EditorialRun) -> tuple[int, ...]:
    values: list[int] = []
    for document in run.evidence.get("documents", []):
        if not isinstance(document, dict):
            continue
        value = document.get("raw_item_id")
        if isinstance(value, int) and value > 0 and value not in values:
            values.append(value)
    if run.raw_item_id not in values:
        values.insert(0, run.raw_item_id)
    return tuple(values)


def _valid_snapshot(snapshot: RawItemMediaSnapshot) -> bool:
    manifest = snapshot.manifest
    if (
        manifest.get("publication_permitted") is not False
        or manifest.get("generated_media_used") is not False
        or manifest.get("candidate_extraction_network_access_performed") is not False
        or manifest.get("source_page") != snapshot.source_page
        or manifest.get("source_html_sha256") != snapshot.source_html_sha256
        or manifest.get("source_html_bytes") != snapshot.source_html_bytes
    ):
        return False
    if "rights_verified" in json.dumps(manifest, ensure_ascii=False):
        return False
    if snapshot.source_html is not None:
        encoded = snapshot.source_html.encode("utf-8")
        if len(encoded) != snapshot.source_html_bytes:
            return False
        if hashlib.sha256(encoded).hexdigest() != snapshot.source_html_sha256:
            return False
    elif manifest.get("candidates"):
        return False
    return True


def build_media_review_packet(session: Session, raw_item_id: int) -> dict[str, Any]:
    """Build pre-download review input from immutable source-page sidecars only."""
    run = session.get(EditorialRun, raw_item_id)
    if run is None:
        return _text_mode(raw_item_id, reason="editorial_run_missing")
    if run.outcome.get("status") != "ready" or run.post_id is None:
        return _text_mode(raw_item_id, reason="editorial_not_ready")
    package_sha256 = run.outcome.get("_package_sha256")
    if not isinstance(package_sha256, str) or len(package_sha256) != 64:
        return _text_mode(raw_item_id, reason="reviewed_package_missing")

    review_candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for document_id in _document_ids(run):
        snapshot = session.get(RawItemMediaSnapshot, document_id)
        if snapshot is None or not _valid_snapshot(snapshot):
            continue
        candidates = snapshot.manifest.get("candidates", [])
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            image_url = candidate.get("url")
            source_page = candidate.get("source_page")
            if not isinstance(image_url, str) or not isinstance(source_page, str):
                continue
            if source_page != snapshot.source_page or candidate.get("event_match") != "unreviewed":
                continue
            key = (image_url, source_page)
            if key in seen:
                continue
            seen.add(key)
            review_candidates.append(
                {
                    "candidate": candidate,
                    "source_raw_item_id": document_id,
                    "source_html_sha256": snapshot.source_html_sha256,
                    "assessment_template": {
                        "image_url": image_url,
                        "source_page": source_page,
                        "kind": "unknown",
                        "event_match": False,
                        "event_year": None,
                        "visual_value": 0,
                        "distinct_visual_value": False,
                        "rights_method": "unknown",
                        "rights_status": "needs_review",
                        "rights_evidence_url": "",
                        "rights_rationale": "",
                        "attribution": candidate.get("attribution") or "",
                        "archive_label": "",
                        "assessed_by": "",
                    },
                }
            )

    if not review_candidates:
        packet = _text_mode(raw_item_id, reason="no_reviewable_media_candidates")
        packet.update(post_id=run.post_id, editorial_package_sha256=package_sha256)
        return packet
    return {
        "version": 1,
        "raw_item_id": raw_item_id,
        "post_id": run.post_id,
        "editorial_package_sha256": package_sha256,
        "mode": "needs_visual_review",
        "reason": "candidate_review_required",
        "candidates": review_candidates,
        "requires_independent_media_reviews": True,
        "download_permitted": False,
        "publication_permitted": False,
        "generated_media_used": False,
    }


def pending_media_review_ids(session: Session, *, limit: int = 20) -> tuple[int, ...]:
    """Materialize the review queue from READY runs plus immutable media sidecars."""
    if not 1 <= limit <= 100:
        raise ValueError("media review queue limit must be between 1 and 100")
    ids: list[int] = []
    runs = session.scalars(select(EditorialRun).order_by(EditorialRun.raw_item_id.desc()))
    for run in runs:
        if build_media_review_packet(session, run.raw_item_id).get("mode") != "needs_visual_review":
            continue
        ids.append(run.raw_item_id)
        if len(ids) >= limit:
            break
    return tuple(ids)


def plan_reviewed_packet(
    packet: dict[str, Any],
    assessments: tuple[CoverAssessment, ...],
    *,
    event_year: int,
    allow_album: bool = False,
) -> dict[str, object]:
    """Apply explicit cover assessments only to candidates bound to this packet."""
    if packet.get("mode") != "needs_visual_review":
        if assessments:
            raise ValueError("text-only packet cannot accept media assessments")
        return {
            "version": 1,
            "mode": "text",
            "selected": [],
            "rejected": [],
            "reason": packet.get("reason", "no_suitable_cover_publish_text"),
            "requires_independent_media_reviews": False,
        }
    known = {
        (
            item.get("candidate", {}).get("url"),
            item.get("candidate", {}).get("source_page"),
        )
        for item in packet.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("candidate"), dict)
    }
    if any((item.image_url, item.source_page) not in known for item in assessments):
        raise ValueError("assessment is not bound to the ingested media snapshot")
    return plan_cover(assessments, event_year=event_year, allow_album=allow_album)


def write_media_review_packet(
    session: Session,
    raw_item_id: int,
    output: Path,
) -> dict[str, Any]:
    packet = build_media_review_packet(session, raw_item_id)
    path = output / f"media-review-{raw_item_id}.json"
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return packet
