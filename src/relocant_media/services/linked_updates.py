"""Канонические ссылки на собственные публичные квитанции, без побочных эффектов."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.linked_update import PublishedPostReference
from relocant_media.ai.package import (
    EditorialPackage,
    canonical_json,
    context_sha256,
    draft_sha256,
    package_coverage_reason,
    package_facts_scope,
)
from relocant_media.domain.channel_targets import APPROVED_CHANNELS_BY_CITY
from relocant_media.domain.policy import requires_human_review
from relocant_media.domain.presentation import TEST_PREFIX
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.revision_control import SourceRevisionMaterialization

MAX_REFERENCES = 8
REFERENCE_WINDOW = timedelta(hours=48)
_PUBLIC_CHANNEL_URLS = {"belgrade": "https://t.me/eduvbelgrad"}


def published_post_reference(
    session: Session, raw_id: int, *, city: str, now: datetime,
) -> PublishedPostReference:
    from relocant_media.ops.gpt_export_receipt import PUBLIC_SOURCE_TYPES
    from relocant_media.services.editorial import _snapshot_member
    from relocant_media.services.gpt_fallback import _registered_public, _registry

    if city not in APPROVED_CHANNELS_BY_CITY or city not in _PUBLIC_CHANNEL_URLS:
        raise ValueError("Город не имеет разрешённого собственного канала")
    run = session.get(EditorialRun, raw_id, populate_existing=True)
    post = session.get(Post, run.post_id, populate_existing=True) if run and run.post_id else None
    raw = session.get(RawItem, raw_id, populate_existing=True)
    source = session.get(Source, raw.source_id, populate_existing=True) if raw else None
    if (run is None or post is None or raw is None or source is None
            or run.delivery_status != "sent" or run.staging_message_id is not None
            or type(post.telegram_message_id) is not int or post.telegram_message_id <= 0
            or post.published_at is None or post.city != city or source.city != city
            or run.evidence.get("city") != city or "_reader_submission_id" in run.outcome
            or source.high_risk or TEST_PREFIX in post.title):
        raise ValueError("Нет собственной подтверждённой публичной публикации")
    published = post.published_at.replace(tzinfo=UTC) if post.published_at.tzinfo is None else (
        post.published_at.astimezone(UTC))
    if not timedelta() <= now.astimezone(UTC) - published <= REFERENCE_WINDOW:
        raise ValueError("Прежняя публикация вне окна связанного обновления")
    package = EditorialPackage.model_validate_json(canonical_json(run.outcome.get("_package")))
    if (package.version != 2 or package.event is None or package.linked_update is not None
            or package.raw_item_id != raw_id or package.city != city
            or (package.source_id, package.article_id) != (source.id, raw.external_id)
            or package_facts_scope(package) not in {"ordinary_local", "confirmed_transport_notice"}
            or package_coverage_reason(package) is not None
            or requires_human_review(package.outcome.ranking.category, package.outcome.confidence)
            or (post.title, post.body) != (package.outcome.draft.title, package.outcome.draft.body)
            or run.evidence.get("event_key") != package.event.event_key
            or session.scalar(select(SourceRevisionMaterialization.raw_item_id).where(
                SourceRevisionMaterialization.raw_item_id == raw_id)) is not None):
        raise ValueError("Прежний публичный контекст не является исходной новостью")
    digest = hashlib.sha256(canonical_json(package.model_dump(mode="json")).encode()).hexdigest()
    if run.outcome.get("_package_sha256") != digest:
        raise ValueError("Изменился пакет прежней публикации")
    if (len({review.reviewer_id.strip().casefold() for review in package.reviews}) != 2 or any(
            review.verdict != "approve" or review.flags or review.score < 75
            or (review.source_id, review.article_id) != (source.id, raw.external_id)
            or not review.live_source_checked or review.context_sha256 != context_sha256(package)
            or review.draft_sha256 != draft_sha256(package.outcome.draft)
            or not timedelta() <= published - review.reviewed_at <= timedelta(
                hours=2 if package.urgent else 24)
            for review in package.reviews)):
        raise ValueError("Прежняя публикация не имеет точных подтверждённых ревью")
    registry = _registry()
    for doc in package.event.documents:
        item = session.get(RawItem, doc.raw_item_id, populate_existing=True)
        policy = session.get(Source, doc.source_id, populate_existing=True)
        config = registry.get(doc.source_id)
        if (item is None or policy is None or config is None
                or doc.high_risk or doc.ownership not in {"official", "media"}
                or config.ownership not in {"official", "media"}
                or policy.source_type not in PUBLIC_SOURCE_TYPES
                or not _registered_public(item, policy, config)
                or canonical_json(doc.model_dump(mode="json"))
                != canonical_json(_snapshot_member(item, policy))):
            raise ValueError("Прежний пакет содержит закрытые или изменённые доказательства")
    return PublishedPostReference.model_validate_json(canonical_json({
        "raw_item_id": raw_id, "message_id": post.telegram_message_id,
        "channel_id": APPROVED_CHANNELS_BY_CITY[city],
        "public_url": f"{_PUBLIC_CHANNEL_URLS[city]}/{post.telegram_message_id}",
        "source_id": source.id, "source_url": raw.url, "source_type": source.source_type,
        "ownership": source.ownership, "high_risk": source.high_risk,
        "auto_publish": source.auto_publish, "title": post.title,
        "event_key": package.event.event_key, "package_sha256": digest,
        "post_sha256": hashlib.sha256(canonical_json({
            "title": post.title, "body": post.body,
        }).encode()).hexdigest(), "published_at": published.isoformat(),
    }))


def published_post_references(
    session: Session, *, city: str, now: datetime,
) -> list[dict[str, Any]]:
    rows = session.scalars(select(EditorialRun.raw_item_id).join(
        Post, Post.id == EditorialRun.post_id,
    ).where(EditorialRun.delivery_status == "sent", Post.city == city,
            Post.published_at >= now - REFERENCE_WINDOW,
            Post.published_at <= now).order_by(Post.published_at.desc(), Post.id.desc()).limit(64))
    result = []
    for raw_id in rows:
        try:
            reference = published_post_reference(session, raw_id, city=city, now=now)
        except ValueError:
            continue
        result.append(reference.model_dump(mode="json"))
        if len(result) == MAX_REFERENCES:
            break
    return result


def validate_linked_update(session: Session, package: EditorialPackage, *, now: datetime) -> None:
    if package.linked_update is None:
        return
    if session.scalar(select(SourceRevisionMaterialization.raw_item_id).where(
            SourceRevisionMaterialization.raw_item_id == package.raw_item_id)) is not None:
        raise ValueError("Нельзя подменить сохранённую связь редакции первоисточника")
    parent = package.linked_update.parent
    if parent.raw_item_id == package.raw_item_id:
        raise ValueError("Новость не может быть собственным обновлением")
    current = published_post_reference(session, parent.raw_item_id, city=package.city, now=now)
    if current != parent:
        raise ValueError("Изменился канонический контекст прежней публикации")
