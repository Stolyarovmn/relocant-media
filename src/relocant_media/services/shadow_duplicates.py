"""Read-only service exposing non-authoritative duplicate signals."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.jev import MAX_DUPLICATE_PAIRS, duplicate_shadow, jev_enabled
from relocant_media.domain.dedup import EventCandidate
from relocant_media.domain.shadow_dedup import (
    DEFAULT_SHADOW_THRESHOLD,
    LexicalSemanticScorer,
    find_shadow_duplicates,
)
from relocant_media.storage.models import RawItem, Source


def _candidate(item: RawItem) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item.id,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        body=item.body,
        published_at=item.published_at,
        canonical_url=item.canonical_url,
        content_sha256=item.content_sha256,
    )


def build_shadow_duplicate_report(
    session: Session,
    *,
    city: str = "belgrade",
    limit: int = 100,
) -> dict[str, Any]:
    """Return possible missed duplicates without changing editorial state."""
    rows = session.execute(
        select(RawItem, Source)
        .join(Source, Source.id == RawItem.source_id)
        .where(Source.city == city)
        .order_by(RawItem.published_at, RawItem.id)
    ).all()
    scorer = LexicalSemanticScorer()
    candidates = [_candidate(item) for item, _source in rows]
    use_jev = jev_enabled()
    if use_jev:
        # One lexical pass also provides a wider, bounded Jev shortlist.
        shortlist = find_shadow_duplicates(
            candidates, scorer=scorer, threshold=0.40, limit=max(limit, 64),
        )
        signals = [
            replace(signal, threshold=DEFAULT_SHADOW_THRESHOLD)
            for signal in shortlist
            if signal.score.score >= DEFAULT_SHADOW_THRESHOLD
        ][:limit]
    else:
        shortlist = []
        signals = find_shadow_duplicates(candidates, scorer=scorer, limit=limit)
    metadata = scorer.metadata
    report: dict[str, Any] = {
        "mode": "shadow_only",
        "authoritative": False,
        "decision_effect": "none",
        "scorer": {
            "id": metadata.scorer_id,
            "version": metadata.scorer_version,
            "embedding_model": metadata.embedding_model,
            "embedding_dimension": metadata.embedding_dimension,
        },
        "signals": [
            {
                "left_raw_item_id": signal.left_raw_item_id,
                "right_raw_item_id": signal.right_raw_item_id,
                "left_source_id": signal.left_source_id,
                "right_source_id": signal.right_source_id,
                "score": signal.score.score,
                "threshold": signal.threshold,
                "scorer": {
                    "id": signal.scorer.scorer_id,
                    "version": signal.scorer.scorer_version,
                    "embedding_model": signal.scorer.embedding_model,
                    "embedding_dimension": signal.scorer.embedding_dimension,
                },
                "components": {
                    "title_overlap": signal.score.title_overlap,
                    "body_overlap": signal.score.body_overlap,
                    "character_similarity": signal.score.character_similarity,
                    "anchor_overlap": signal.score.anchor_overlap,
                },
            }
            for signal in signals
        ],
    }
    if use_jev:
        # No private submissions, community tips, or unapproved sources leave the DB.
        official = {
            item.id: (item, source)
            for item, source in rows
            if source.ownership == "official"
        }
        pairs: list[dict[str, Any]] = []
        for signal in shortlist:
            left = official.get(signal.left_raw_item_id)
            right = official.get(signal.right_raw_item_id)
            if left is None or right is None:
                continue
            pairs.append({
                "left_raw_item_id": signal.left_raw_item_id,
                "right_raw_item_id": signal.right_raw_item_id,
                "left_title": left[0].title,
                "left_body": left[0].body,
                "right_title": right[0].title,
                "right_body": right[0].body,
            })
            if len(pairs) >= MAX_DUPLICATE_PAIRS:
                break
        try:
            report["jev_shadow"] = duplicate_shadow(pairs, city=city)
        except Exception as error:
            report["jev_shadow"] = {
                "mode": "shadow_only",
                "decision_effect": "none",
                "status": "unavailable",
                "error_type": type(error).__name__,
            }
    return report

