"""Observe changes to already published sources; never rewrite evidence or Telegram posts."""
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


def published_sources(session: Session, *, now: datetime, limit: int = 10) -> list[RawItem]:
    return list(session.scalars(select(RawItem).join(EditorialRun,
        EditorialRun.raw_item_id == RawItem.id).join(Post, Post.id == EditorialRun.post_id).where(
            Post.city == "belgrade", Post.published_at >= now-timedelta(days=30),
            EditorialRun.delivery_status == "sent"
        ).order_by(Post.published_at.desc()).limit(limit)))


def recent_source_load(session: Session, *, now: datetime) -> dict[str, int]:
    return dict(session.execute(select(Source.id, func.count(Post.id)).join(RawItem,
        RawItem.source_id == Source.id).join(EditorialRun,
        EditorialRun.raw_item_id == RawItem.id).join(Post, Post.id == EditorialRun.post_id).where(
            Post.city == "belgrade", Post.published_at >= now-timedelta(hours=24),
            EditorialRun.delivery_status == "sent").group_by(Source.id)).all())


def evidence_changed(original: RawItem, current: Any) -> bool:
    return any(getattr(original, key) != getattr(current, key)
               for key in ("url", "title", "body", "published_at", "published_at_precision"))
