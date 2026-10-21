"""Read-only newsroom control projection over the existing durable editorial state."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.domain.dedup import EventCandidate, cluster_events, event_key
from relocant_media.storage.models import (
    CollectorFailure,
    EditorialRun,
    Post,
    RawItem,
    Source,
    SourceRevision,
    Submission,
)


class NewsroomState(StrEnum):
    DISCOVERED = "discovered"
    EVIDENCE_READY = "evidence_ready"
    DRAFTING = "drafting"
    REVIEW = "review"
    READY = "ready"
    SENDING = "sending"
    SENT = "sent"
    HELD = "held"
    NEEDS_REVERIFY = "needs_reverify"
    EXPIRED = "expired"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


_STATE_PRIORITY = {
    NewsroomState.UNCERTAIN: 0,
    NewsroomState.NEEDS_REVERIFY: 1,
    NewsroomState.SENDING: 2,
    NewsroomState.FAILED: 3,
    NewsroomState.DRAFTING: 4,
    NewsroomState.REVIEW: 5,
    NewsroomState.READY: 6,
    NewsroomState.EVIDENCE_READY: 7,
    NewsroomState.DISCOVERED: 8,
    NewsroomState.HELD: 9,
    NewsroomState.EXPIRED: 10,
    NewsroomState.SENT: 11,
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized is not None else None


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


def _reason(run: EditorialRun | None) -> str | None:
    if run is None:
        return None
    value = run.outcome.get("reason")
    return value if isinstance(value, str) and value else None


def _review_status(run: EditorialRun | None, post: Post | None) -> str:
    if post is not None and post.status:
        return post.status
    if run is None:
        return "not_started"
    value = run.outcome.get("status")
    return value if isinstance(value, str) and value else "unknown"


def _is_expired(reason: str | None) -> bool:
    if reason is None:
        return False
    normalized = reason.casefold()
    return any(token in normalized for token in ("expired", "stale", "past_event", "ended"))


def _state(
    *,
    anchor: RawItem,
    source: Source,
    run: EditorialRun | None,
    post: Post | None,
    revisions: list[SourceRevision],
) -> tuple[NewsroomState, str]:
    reason = _reason(run)
    if run is not None:
        # Delivery reconciliation always outranks editorial re-verification. If Telegram may
        # already have accepted a message, the first operator action must be to reconcile it.
        if run.delivery_status == "uncertain":
            return NewsroomState.UNCERTAIN, "delivery_uncertain"
        if run.delivery_status == "sending":
            return NewsroomState.SENDING, "delivery_reconciliation_required"

        created_at = _utc(run.created_at)
        if created_at is not None and any(
            (_utc(revision.observed_at) or created_at) > created_at for revision in revisions
        ):
            return NewsroomState.NEEDS_REVERIFY, "source_revision_after_review"
        if run.delivery_status == "sent" or (
            post is not None and post.telegram_message_id is not None
        ):
            return NewsroomState.SENT, "sent"

        status = _review_status(run, post)
        if status in {"failed", "error"}:
            return NewsroomState.FAILED, reason or "editorial_failed"
        if status in {"processing", "drafting"}:
            return NewsroomState.DRAFTING, reason or "editorial_in_progress"
        if status == "ready":
            return NewsroomState.READY, reason or "ready_for_delivery"
        if status == "human_review":
            return NewsroomState.REVIEW, reason or "review_required"
        if _is_expired(reason):
            return NewsroomState.EXPIRED, reason or "expired"
        return NewsroomState.HELD, reason or status or "held"

    if not source.enabled:
        return NewsroomState.HELD, "source_disabled"
    if anchor.canonical_url and anchor.content_sha256:
        return NewsroomState.EVIDENCE_READY, "awaiting_editorial"
    return NewsroomState.DISCOVERED, "awaiting_normalization"


def _retry_info(
    state: NewsroomState,
    last_failure: dict[str, object] | None,
) -> dict[str, object]:
    if state in {NewsroomState.SENDING, NewsroomState.UNCERTAIN}:
        return {
            "automatic_retry": False,
            "policy": "manual_reconciliation",
        }
    if state == NewsroomState.NEEDS_REVERIFY:
        return {
            "automatic_retry": False,
            "policy": "fresh_review_required",
        }
    if state == NewsroomState.FAILED:
        return {
            "automatic_retry": True,
            "policy": "scheduler_or_operator",
        }
    if last_failure is not None and state in {
        NewsroomState.DISCOVERED,
        NewsroomState.EVIDENCE_READY,
        NewsroomState.HELD,
    }:
        return {
            "automatic_retry": True,
            "policy": "scheduler_or_operator",
        }
    return {"automatic_retry": False, "policy": "none"}


def build_newsroom_inbox(
    session: Session,
    *,
    city: str = "belgrade",
    limit: int = 100,
) -> dict[str, Any]:
    """Build an event-level control inbox without mutating any editorial state."""
    if limit < 1:
        raise ValueError("limit must be positive")

    rows = session.execute(
        select(RawItem, Source)
        .join(Source, Source.id == RawItem.source_id)
        .where(Source.city == city)
        .order_by(RawItem.id)
    ).all()
    if not rows:
        return {
            "version": 1,
            "city": city,
            "generated_at": datetime.now(UTC).isoformat(),
            "counts": {},
            "total_events": 0,
            "returned_events": 0,
            "items": [],
        }

    items_by_id = {item.id: (item, source) for item, source in rows}
    sources_by_id = {source.id: source for _item, source in rows}
    raw_ids = list(items_by_id)
    source_ids = set(sources_by_id)

    runs = {
        run.raw_item_id: run
        for run in session.scalars(
            select(EditorialRun).where(EditorialRun.raw_item_id.in_(raw_ids))
        )
    }
    post_ids = {run.post_id for run in runs.values() if run.post_id is not None}
    posts = (
        {post.id: post for post in session.scalars(select(Post).where(Post.id.in_(post_ids)))}
        if post_ids
        else {}
    )

    revisions_by_raw: dict[int, list[SourceRevision]] = defaultdict(list)
    for revision in session.scalars(
        select(SourceRevision)
        .where(SourceRevision.raw_item_id.in_(raw_ids))
        .order_by(SourceRevision.observed_at, SourceRevision.id)
    ):
        revisions_by_raw[revision.raw_item_id].append(revision)

    submissions_by_raw: dict[int, list[Submission]] = defaultdict(list)
    for submission in session.scalars(select(Submission).order_by(Submission.id)):
        if submission.payload.get("city") != city:
            continue
        candidate_ids = submission.payload.get("raw_item_ids", [])
        if not isinstance(candidate_ids, list):
            continue
        for raw_id in candidate_ids:
            if type(raw_id) is int and raw_id in items_by_id:
                submissions_by_raw[raw_id].append(submission)

    last_failure_by_source: dict[str, CollectorFailure] = {}
    for failure in session.scalars(
        select(CollectorFailure)
        .where(CollectorFailure.source_id.in_(source_ids))
        .order_by(CollectorFailure.occurred_at, CollectorFailure.id)
    ):
        last_failure_by_source[failure.source_id] = failure

    clusters = cluster_events([_candidate(item) for item, _source in rows])
    control_items: list[dict[str, Any]] = []
    for cluster in clusters:
        member_ids = [candidate.raw_item_id for candidate in cluster]
        reviewed_member = next(
            (candidate for candidate in cluster if candidate.raw_item_id in runs),
            None,
        )
        anchor_candidate = reviewed_member or cluster[0]
        anchor, anchor_source = items_by_id[anchor_candidate.raw_item_id]
        run = runs.get(anchor.id)
        post = posts.get(run.post_id) if run is not None and run.post_id is not None else None

        revisions = [
            revision
            for raw_id in member_ids
            for revision in revisions_by_raw.get(raw_id, [])
        ]
        state, reason_code = _state(
            anchor=anchor,
            source=anchor_source,
            run=run,
            post=post,
            revisions=revisions,
        )

        submissions = {
            submission.id: submission
            for raw_id in member_ids
            for submission in submissions_by_raw.get(raw_id, [])
        }
        member_source_ids = {items_by_id[raw_id][1].id for raw_id in member_ids}
        source_summaries = [
            {
                "source_id": sources_by_id[source_id].id,
                "name": sources_by_id[source_id].name,
                "ownership": sources_by_id[source_id].ownership,
                "high_risk": sources_by_id[source_id].high_risk,
                "auto_publish": sources_by_id[source_id].auto_publish,
                "enabled": sources_by_id[source_id].enabled,
            }
            for source_id in sorted(member_source_ids)
        ]

        failure_candidates = [
            last_failure_by_source[source_id]
            for source_id in member_source_ids
            if source_id in last_failure_by_source
        ]
        latest_failure = (
            max(
                failure_candidates,
                key=lambda failure: _utc(failure.occurred_at)
                or datetime.min.replace(tzinfo=UTC),
            )
            if failure_candidates
            else None
        )
        failure_summary: dict[str, object] | None = None
        if latest_failure is not None:
            failure_summary = {
                "source_id": latest_failure.source_id,
                "strategy": latest_failure.strategy,
                "error_type": latest_failure.error_type,
                "occurred_at": _iso(latest_failure.occurred_at),
            }

        stored_event_key = run.evidence.get("event_key") if run is not None else None
        resolved_event_key = (
            stored_event_key
            if isinstance(stored_event_key, str) and stored_event_key
            else event_key(anchor_candidate)
        )
        evidence_ids = member_ids
        if run is not None:
            documents = run.evidence.get("documents")
            if isinstance(documents, list):
                stored_ids: list[int] = []
                for document in documents:
                    if not isinstance(document, dict):
                        continue
                    raw_id = document.get("raw_item_id")
                    if type(raw_id) is int:
                        stored_ids.append(raw_id)
                if stored_ids:
                    evidence_ids = stored_ids

        control_items.append(
            {
                "event_key": resolved_event_key,
                "state": state.value,
                "reason_code": reason_code,
                "raw_item_ids": member_ids,
                "anchor_raw_item_id": anchor.id,
                "title": anchor.title,
                "published_at": _iso(anchor.published_at),
                "review_status": _review_status(run, post),
                "delivery_status": run.delivery_status if run is not None else "not_started",
                "post_id": post.id if post is not None else None,
                "telegram_message_id": post.telegram_message_id if post is not None else None,
                "editorial_rule_version": (
                    run.editorial_rule_version if run is not None else None
                ),
                "sources": source_summaries,
                "evidence": {
                    "raw_item_ids": evidence_ids,
                    "document_count": len(evidence_ids),
                },
                "source_revision_count": len(revisions),
                "submissions": [
                    {
                        "id": submission.id,
                        "kind": submission.kind,
                        "status": submission.status,
                        "created_at": _iso(submission.created_at),
                    }
                    for submission in sorted(submissions.values(), key=lambda item: item.id)
                ],
                "last_error": failure_summary,
                "retry": _retry_info(state, failure_summary),
            }
        )

    control_items.sort(
        key=lambda item: (
            _STATE_PRIORITY[NewsroomState(item["state"])],
            item["published_at"] or "",
            item["anchor_raw_item_id"],
        )
    )
    visible = control_items[:limit]
    counts = Counter(item["state"] for item in control_items)
    return {
        "version": 1,
        "city": city,
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": dict(sorted(counts.items())),
        "total_events": len(control_items),
        "returned_events": len(visible),
        "items": visible,
    }
