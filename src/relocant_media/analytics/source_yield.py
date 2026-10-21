"""Read-only source funnel analytics from persisted newsroom state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source-yield timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def build_source_yield_report(
    session: Session,
    *,
    city: str,
    since: datetime,
    until: datetime,
) -> dict[str, Any]:
    """Build an anchor-source funnel without changing editorial state.

    The window is based on RawItem.ingested_at. A raw item is counted once per stage because
    EditorialRun.raw_item_id is the run primary key and each run can reference at most one post.
    Public delivery requires the production-channel receipt stored on Post; staging receipts are
    reported separately and are never counted as public sends.
    """
    start = _utc(since)
    end = _utc(until)
    if start >= end:
        raise ValueError("source-yield since must be before until")

    sources = session.scalars(
        select(Source).where(Source.city == city).order_by(Source.id)
    ).all()
    counters: dict[str, dict[str, Any]] = {
        source.id: {
            "source_id": source.id,
            "source_name": source.name,
            "enabled": source.enabled,
            "found": 0,
            "editorial_runs": 0,
            "post_created": 0,
            "ready": 0,
            "staging_sent": 0,
            "public_sent": 0,
        }
        for source in sources
    }

    rows = session.execute(
        select(RawItem, EditorialRun, Post)
        .join(Source, Source.id == RawItem.source_id)
        .outerjoin(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .outerjoin(Post, Post.id == EditorialRun.post_id)
        .where(
            Source.city == city,
            RawItem.ingested_at >= start,
            RawItem.ingested_at < end,
        )
        .order_by(RawItem.id)
    ).all()

    for item, run, post in rows:
        source = counters.setdefault(
            item.source_id,
            {
                "source_id": item.source_id,
                "source_name": item.source_id,
                "enabled": True,
                "found": 0,
                "editorial_runs": 0,
                "post_created": 0,
                "ready": 0,
                "staging_sent": 0,
                "public_sent": 0,
            },
        )
        source["found"] += 1
        if run is None:
            continue
        source["editorial_runs"] += 1
        if post is None:
            continue
        source["post_created"] += 1
        if post.status == "ready":
            source["ready"] += 1
        if (
            run.delivery_status == "sent"
            and isinstance(run.staging_message_id, int)
            and run.staging_message_id > 0
        ):
            source["staging_sent"] += 1
        if (
            run.delivery_status == "sent"
            and isinstance(post.telegram_message_id, int)
            and post.telegram_message_id > 0
        ):
            source["public_sent"] += 1

    source_rows: list[dict[str, Any]] = []
    for source_id in sorted(counters):
        row = counters[source_id]
        found = int(row["found"])
        editorial_runs = int(row["editorial_runs"])
        posts = int(row["post_created"])
        ready = int(row["ready"])
        public_sent = int(row["public_sent"])
        source_rows.append(
            {
                **row,
                "rates": {
                    "reviewed_from_found": _rate(editorial_runs, found),
                    "post_from_found": _rate(posts, found),
                    "ready_from_found": _rate(ready, found),
                    "public_sent_from_found": _rate(public_sent, found),
                    "public_sent_from_ready": _rate(public_sent, ready),
                },
            }
        )

    totals = {
        key: sum(int(row[key]) for row in source_rows)
        for key in (
            "found",
            "editorial_runs",
            "post_created",
            "ready",
            "staging_sent",
            "public_sent",
        )
    }
    return {
        "city": city,
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "attribution": "anchor_source",
        "sources": source_rows,
        "totals": {
            **totals,
            "rates": {
                "reviewed_from_found": _rate(totals["editorial_runs"], totals["found"]),
                "post_from_found": _rate(totals["post_created"], totals["found"]),
                "ready_from_found": _rate(totals["ready"], totals["found"]),
                "public_sent_from_found": _rate(totals["public_sent"], totals["found"]),
                "public_sent_from_ready": _rate(totals["public_sent"], totals["ready"]),
            },
        },
        "limitations": [
            "Attribution uses the EditorialRun anchor raw item; supporting event sources are not credited.",
            "The window is based on raw_item.ingested_at, not source publication time.",
            "Public sends require delivery_status=sent and a positive Post.telegram_message_id.",
            "Staging receipts are reported separately and never counted as public sends.",
            "Rates are descriptive source yield and do not measure causal source quality.",
        ],
    }
