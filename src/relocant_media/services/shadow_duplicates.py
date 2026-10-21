"""Read-only service exposing non-authoritative duplicate signals."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.domain.dedup import EventCandidate
from relocant_media.domain.shadow_dedup import find_shadow_duplicates
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
    signals = find_shadow_duplicates(
        [_candidate(item) for item, _source in rows],
        limit=limit,
    )
    return {
        "mode": "shadow_only",
        "authoritative": False,
        "decision_effect": "none",
        "signals": [
            {
                "left_raw_item_id": signal.left_raw_item_id,
                "right_raw_item_id": signal.right_raw_item_id,
                "left_source_id": signal.left_source_id,
                "right_source_id": signal.right_source_id,
                "score": signal.score.score,
                "threshold": signal.threshold,
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
