"""Конечные маршруты материалов внутри существующего канонического писателя.

Ledger использует уже имеющийся SchedulerRun. Функции не делают commit: вызывающий
канонический процесс сохраняет изменения до внешнего действия и после его результата.
Ни одна функция не одобряет публикацию и не повторяет неопределённую доставку.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.road_work_context import road_work_context
from relocant_media.collectors.bgsaobracaj_notices import SOURCE_ID as SECRETARIAT_SOURCE_ID
from relocant_media.collectors.bgsaobracaj_notices import match_bgsaobracaj_notice
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.flight_notices import FLIGHT_REVIEW_REASON
from relocant_media.domain.freshness import FreshnessDecision, evidence_freshness
from relocant_media.domain.policy import (
    AUTO_PUBLISH_CATEGORIES,
    HIGH_RISK_CATEGORIES,
    OWNER_DECISION_REASONS,
    SOURCE_REPLACED_REASON,
)
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import (
    EditorialRun,
    Post,
    RawItem,
    Source,
    SourceRevision,
    Submission,
)

POLICY_VERSION = "material-lifecycle-v1"
TASK_TYPE = "material_lifecycle"
MATERIAL_TTL = timedelta(hours=6)
ATTEMPT_LEASE = timedelta(minutes=15)
RETRY_DELAY = timedelta(minutes=5)
FALLBACK_WAIT = timedelta(minutes=30)
PRIMARY_ATTEMPTS = 2
FALLBACK_ATTEMPTS = 1
AUDIT_ATTEMPTS = 2
DELIVERY_ATTEMPTS = 2
MAX_HISTORY = 20
TERMINAL_STATES = frozenset({"published", "blocked"})
PUBLISHED_TRANSPORT_DUPLICATE = "lifecycle_published_transport_duplicate"


class LifecycleBlockedError(ValueError):
    pass


@dataclass(frozen=True)
class LifecycleSweep:
    due_ids: tuple[int, ...]
    rejection_audit_ids: tuple[int, ...]
    fallback_ids: tuple[int, ...]
    excluded_ids: frozenset[int]
    blocked_ids: tuple[int, ...]


@dataclass
class _SweepCache:
    objects: dict[int, tuple[RawItem, Source, EditorialRun | None, Post | None]]
    records: dict[str, SchedulerRun]
    owner_reviews: list[Submission]
    reviewed_targets: list[tuple[RawItem, Source, EditorialRun, Post | None]]


def _now(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Нужно время с часовым поясом")
    return now.astimezone(UTC)


def _stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Повреждён срок жизненного цикла")
    return _now(datetime.fromisoformat(value))


def _key(city: str, raw_id: int) -> str:
    return f"material-lifecycle:{city}:{raw_id}"


def _objects(
    session: Session, raw_id: int,
) -> tuple[RawItem, Source, EditorialRun | None, Post | None]:
    cache = cast(_SweepCache | None, session.info.get("material_lifecycle_cache"))
    if cache is not None and raw_id in cache.objects:
        return cache.objects[raw_id]
    raw = session.get(RawItem, raw_id)
    source = session.get(Source, raw.source_id) if raw else None
    if raw is None or source is None:
        raise ValueError("Материал или источник отсутствует")
    run = session.get(EditorialRun, raw_id)
    post = session.get(Post, run.post_id) if run and run.post_id else None
    return raw, source, run, post


def _fingerprint(raw: RawItem) -> str:
    payload = [raw.source_id, raw.url, raw.title, raw.body,
               raw.published_at.isoformat() if raw.published_at else None,
               raw.published_at_precision]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _fresh(raw: RawItem, source: Source, now: datetime) -> FreshnessDecision:
    return evidence_freshness(
        raw.title, raw.body, raw.published_at, now, precision=raw.published_at_precision,
        source_type=source.source_type, verified_at=raw.ingested_at,
    )


def _has_receipt(run: EditorialRun | None, post: Post | None) -> bool:
    return bool(run and run.staging_message_id is not None or post and (
        post.telegram_message_id is not None or post.published_at is not None))


def _published(run: EditorialRun | None, post: Post | None) -> bool:
    return bool(run and run.delivery_status == "sent" and post
                and type(post.telegram_message_id) is int and post.telegram_message_id > 0)


def _unsafe_delivery(run: EditorialRun | None, post: Post | None) -> bool:
    return bool(run and (run.delivery_status in {"sending", "uncertain", "sent"}
                        or _has_receipt(run, post)))


def _owner_approved(session: Session, raw_id: int, run: EditorialRun | None) -> bool:
    if run is None:
        return False
    package_sha = run.outcome.get("_package_sha256")
    if not isinstance(package_sha, str) or len(package_sha) != 64:
        return False
    for row in _owner_reviews(session):
        approved = row.status == "approved"
        if (row.status == "publish_blocked" and row.payload.get("owner_decision") == "publish"
                and not any(key in row.payload for key in ("publish_block_reason",
                    "publish_claimed_at", "publish_recovered_at", "telegram_channel_message_id"))):
            record = _read_record(session, raw_id)
            approved = bool(record and record.details.get("validated_legacy_review_id") == row.id)
        if (approved and row.payload.get("raw_item_id") == raw_id
                and row.payload.get("package_sha256") == package_sha):
            return True
    return False


def _owner_reviews(session: Session) -> list[Submission]:
    cache = cast(_SweepCache | None, session.info.get("material_lifecycle_cache"))
    if cache is not None:
        return cache.owner_reviews
    return list(session.scalars(select(Submission).where(
        Submission.kind == "owner_editorial_review",
        Submission.status.in_({"approved", "pending", "revision_requested", "postponed",
                               "publish_blocked"}),
    )))


def _find_record(session: Session, city: str, raw_id: int) -> SchedulerRun | None:
    cache = cast(_SweepCache | None, session.info.get("material_lifecycle_cache"))
    if cache is not None:
        return cache.records.get(_key(city, raw_id))
    return session.scalar(select(SchedulerRun).where(
        SchedulerRun.task_key == _key(city, raw_id),
        SchedulerRun.task_type == TASK_TYPE,
    ).order_by(SchedulerRun.id.desc()).limit(1))


def _record(session: Session, raw: RawItem, source: Source, now: datetime) -> SchedulerRun:
    record = _find_record(session, source.city, raw.id)
    if record is not None:
        root_id = record.details.get("lifecycle_root_raw_id")
        if type(root_id) is int and root_id != raw.id:
            root = _find_record(session, source.city, root_id)
            if root is not None and not root.details.get("lifecycle_root_raw_id"):
                return root
        return record
    record = SchedulerRun(
        task_key=_key(source.city, raw.id), task_type=TASK_TYPE, city=source.city,
        scheduled_for=now, started_at=now, status="waiting", attempt=1,
        details={"schema_version": 1, "policy_version": POLICY_VERSION,
                 "raw_item_id": raw.id, "state": "waiting", "next_action": "prepare",
                 "next_attempt_at": now.isoformat(), "deadline_at": (now+MATERIAL_TTL).isoformat(),
                 "first_seen_at": now.isoformat(), "attempts": 0, "fallback_attempts": 0,
                 "rejection_audits": 0, "audit_attempts": 0, "delivery_attempts": 0,
                 "reason": "awaiting_first_attempt", "fingerprint": _fingerprint(raw),
                 "fingerprints": {str(raw.id): _fingerprint(raw)}, "history": []},
    )
    session.add(record)
    cache = cast(_SweepCache | None, session.info.get("material_lifecycle_cache"))
    if cache is not None:
        cache.records[record.task_key] = record
    return record


def _set(record: SchedulerRun, *, now: datetime, state: str, action: str | None,
         reason: str, next_at: datetime | None = None, **fields: Any) -> None:
    data = dict(record.details)
    changed = any(data.get(key) != value for key, value in (
        ("state", state), ("next_action", action), ("reason", reason)))
    history = data.get("history", [])
    history = list(history) if isinstance(history, list) else []
    if changed:
        history.append({"at": now.isoformat(), "state": state,
                        "next_action": action, "reason": reason})
    data.update(state=state, next_action=action, reason=reason,
                next_attempt_at=next_at.isoformat() if next_at else None,
                updated_at=now.isoformat(), history=history[-MAX_HISTORY:], **fields)
    record.details = data
    record.status = state
    if state in TERMINAL_STATES:
        record.finished_at = now


def _block(session: Session, record: SchedulerRun, run: EditorialRun | None,
           post: Post | None, *, now: datetime, reason: str) -> None:
    _set(record, now=now, state="blocked", action=None, reason=reason)
    raw_ids = {value for value in (record.details.get("raw_item_id"),
               record.details.get("replacement_raw_item_id")) if type(value) is int}
    for review in _owner_reviews(session):
        if (review.status in {"pending", "revision_requested", "postponed"}
                and review.payload.get("raw_item_id") in raw_ids):
            review.payload = {**review.payload, "_lifecycle_closure": {
                "previous_status": review.status, "reason": reason, "at": now.isoformat(),
            }}
            review.status = "cancelled"
    # Транспортную квитанцию, sending/uncertain и согласование владельца не переписываем.
    if (run is not None and run.outcome.get("reason") != SOURCE_REPLACED_REASON
            and not _owner_approved(session, run.raw_item_id, run)
            and run.delivery_status in {"pending", "cancelled"} and not _has_receipt(
        run, post,
    )):
        old = run.outcome
        run.outcome = {**old, "status": "blocked", "reason": "lifecycle_"+reason,
                       "_lifecycle_block": {"policy_version": POLICY_VERSION,
                           "at": now.isoformat(), "previous_status": old.get("status"),
                           "previous_reason": old.get("reason")}}
        if post is not None:
            post.status = "blocked"


def _validate(data: dict[str, Any]) -> bool:
    try:
        if data.get("state") == "processing":
            _stamp(data["attempt_deadline_at"])
        if data.get("state") == "waiting_fallback":
            _stamp(data["fallback_deadline_at"])
        return (data.get("schema_version") == 1 and data.get("policy_version") == POLICY_VERSION
                and type(data.get("attempts")) is int and 0 <= data["attempts"] <= PRIMARY_ATTEMPTS
                and type(data.get("fallback_attempts")) is int
                and 0 <= data["fallback_attempts"] <= FALLBACK_ATTEMPTS
                and type(data.get("flight_review_attempts", 0)) is int
                and 0 <= data.get("flight_review_attempts", 0) <= 1
                and type(data.get("rejection_audits")) is int
                and 0 <= data["rejection_audits"] <= AUDIT_ATTEMPTS
                and type(data.get("audit_attempts")) is int
                and 0 <= data["audit_attempts"] <= AUDIT_ATTEMPTS
                and type(data.get("delivery_attempts")) is int
                and 0 <= data["delivery_attempts"] <= DELIVERY_ATTEMPTS
                and data.get("state") in {"waiting", "processing", "ready", "waiting_fallback"}
                and data.get("next_action") in {"prepare", "retry_prepare", "repair",
                    "fallback", "process", "audit_rejection", "deliver", "owner_delivery",
                    "owner_approval"}
                and _stamp(data["next_attempt_at"]) <= _stamp(data["deadline_at"])
                and _stamp(data["deadline_at"]) >= _stamp(data["first_seen_at"]))
    except (ValueError, KeyError, TypeError):
        return False


def _stored_coverage_reason(run: EditorialRun | None) -> str | None:
    if run is None or "_package" not in run.outcome:
        return None
    from relocant_media.ai.package import (
        EditorialPackage,
        canonical_json,
        package_coverage_reason,
    )

    payload = run.outcome["_package"]
    if hashlib.sha256(canonical_json(payload).encode()).hexdigest() != (
            run.outcome.get("_package_sha256")):
        return "invalid_reviewed_package"
    try:
        package = EditorialPackage.model_validate_json(json.dumps(payload))
    except ValueError:
        return "invalid_reviewed_package"
    return package_coverage_reason(package)


def _reviewable_utility(
    source: Source, run: EditorialRun, post: Post | None, reason: Any,
) -> bool:
    ranking = run.outcome.get("ranking")
    draft = run.outcome.get("draft")
    return (isinstance(ranking, dict) and ranking.get("category") == "utilities"
            and source.enabled and source.ownership == "official" and source.auto_publish
            and not source.high_risk and post is not None and post.category == "utilities"
            and isinstance(draft, dict) and (draft.get("title"), draft.get("body"))
            == (post.title, post.body)
            and run.delivery_status == "pending" and not _unsafe_delivery(run, post)
            and isinstance(reason, str) and reason in OWNER_DECISION_REASONS
            and "_package" in run.outcome and _stored_coverage_reason(run) is None)


def _wait_for_owner(record: SchedulerRun, *, now: datetime) -> None:
    if record.details.get("next_action") != "owner_approval":
        _set(record, now=now, state="waiting", action="owner_approval",
             reason="waiting_for_owner_approval", next_at=_stamp(record.details["deadline_at"]))


def _restore_unqueued_utility(
    session: Session, record: SchedulerRun, *, now: datetime,
) -> None:
    """Repair only a false terminal hold before an owner card ever existed."""
    data = record.details
    raw_id = data.get("raw_item_id")
    if (type(raw_id) is not int or data.get("lifecycle_root_raw_id")
            or data.get("replacement_raw_item_id")
            or not isinstance(data.get("deadline_at"), str)
            or not _validate({**data, "state": "waiting", "next_action": "owner_approval",
                             "next_attempt_at": data["deadline_at"]})
            or now >= _stamp(data["deadline_at"])):
        return
    try:
        raw, source, run, post = _objects(session, raw_id)
        fresh = _fresh(raw, source, now)
    except ValueError:
        return
    old = run.outcome.get("_lifecycle_block") if run is not None else None
    if (run is None or post is None or post.status != "blocked"
            or run.outcome.get("status") != "blocked"
            or run.outcome.get("reason") != "lifecycle_separate_publication_approval_required"
            or not isinstance(old, dict) or old.get("previous_status") != "human_review"
            or old.get("policy_version") != POLICY_VERSION
            or not _reviewable_utility(source, run, post, old.get("previous_reason"))
            or data.get("fingerprints", {}).get(str(raw_id)) != _fingerprint(raw)
            or fresh.action != "keep"):
        return
    if session.scalar(select(Submission.id).where(
        Submission.kind == "owner_editorial_review",
        Submission.payload["raw_item_id"].as_integer() == raw_id,
    ).limit(1)) is not None:
        return
    outcome = {key: value for key, value in run.outcome.items() if key != "_lifecycle_block"}
    run.outcome = {**outcome, "status": "human_review", "reason": old["previous_reason"],
                   "_recovered_owner_hold": old}
    post.status = "human_review"
    _wait_for_owner(record, now=now)
    record.finished_at = None


def _reopen_unexpired_road_work(
    session: Session, record: SchedulerRun, *, now: datetime,
) -> None:
    """Archive one provably premature rejection before any delivery or owner action."""
    from relocant_media.ai.package import (
        EditorialPackage,
        canonical_json,
        context_sha256,
        draft_sha256,
        package_coverage_reason,
    )
    from relocant_media.domain.dedup import event_key
    from relocant_media.services.editorial import (
        _candidate,
        _document,
        _event_items,
        _snapshot_member,
    )

    lock_editorial_city(session, record.city)
    session.refresh(record)
    data = record.details
    raw_id = data.get("raw_item_id")
    if (record.status != "blocked" or data.get("state") != "blocked"
            or data.get("reason") != "draft_coverage_ungrounded" or record.city != "belgrade"
            or type(raw_id) is not int or data.get("replacement_raw_item_id") is not None
            or data.get("lifecycle_root_raw_id") is not None or "road_work_reopened_at" in data
            or data.get("attempts") != 1 or data.get("fallback_attempts") != 0
            or data.get("delivery_attempts") != 0 or data.get("attempt_records")
            or not _validate({**data, "state": "waiting", "next_action": "prepare",
                              "next_attempt_at": now.isoformat()})
            or now >= _stamp(data["deadline_at"])):
        return
    try:
        raw, source, run, post = _objects(session, raw_id)
        if (source.id != SECRETARIAT_SOURCE_ID or source.ownership != "official"
                or not source.enabled or not source.auto_publish or source.high_risk
                or not match_bgsaobracaj_notice(raw.url)
                or data.get("fingerprints", {}).get(str(raw_id)) != _fingerprint(raw)
                or _fresh(raw, source, now).action != "keep"
                or run is None or run.delivery_status != "pending" or _has_receipt(run, post)
                or post is None or post.status != "blocked" or post.category != "transport"
                or run.outcome.get("status") != "blocked"
                or run.outcome.get("reason") != "lifecycle_draft_coverage_ungrounded"
                or {key for key in run.outcome if key.startswith("_")} != {
                    "_package", "_package_sha256", "_lifecycle_block",
                }):
            return
        block = run.outcome["_lifecycle_block"]
        if (not isinstance(block, dict) or block.get("policy_version") != POLICY_VERSION
                or block.get("previous_status") != "human_review"
                or block.get("previous_reason") != "draft_coverage_ungrounded"):
            return
        payload = canonical_json(run.outcome["_package"])
        if hashlib.sha256(payload.encode()).hexdigest() != run.outcome["_package_sha256"]:
            return
        package = EditorialPackage.model_validate_json(payload)
        current_evidence = _document(raw).model_dump(mode="json")
        if (package.version != 2 or package.event is None or package.city != source.city
                or package.event.event_key != event_key(_candidate(raw))
                or (package.raw_item_id, package.source_id, package.article_id)
                    != (raw_id, raw.source_id, raw.external_id)
                or package.evidence.model_dump(mode="json") != current_evidence
                or package.outcome.ranking.category != "transport"
                or (post.title, post.body) != (
                    package.outcome.draft.title, package.outcome.draft.body)
                or package_coverage_reason(package) != "draft_coverage_ungrounded"):
            return
        blocked_at = _stamp(block["at"])
        facts = next(review for review in package.reviews if review.role == "facts")
        utility = next(review for review in package.reviews if review.role == "utility")
        expired_flags = {"expired_event", "истёкший срок события", "истекший срок события"}
        if (facts.verdict != "hold" or set(facts.flags) != {
                "expired_event", "draft_coverage_ungrounded",
        } or utility.verdict not in {"hold", "reject"} or not utility.flags
                or not {flag.casefold() for flag in utility.flags} <= expired_flags
                or any(not review.live_source_checked
                 or review.reviewed_at > blocked_at
                 or review.context_sha256 != context_sha256(package)
                 or review.draft_sha256 != draft_sha256(package.outcome.draft)
                 for review in package.reviews)
                or blocked_at > now):
            return
        members = _event_items(session, anchor=raw, anchor_source=source)
        exact = [_snapshot_member(member, policy) for member, policy in members]
        if (len(exact) != 1 or run.evidence.get("documents") != exact
                or [doc.model_dump(mode="json") for doc in package.event.documents] != exact):
            return
        if not any(window["status"] in {"active", "upcoming"}
                   for clause in road_work_context((package.evidence,), now, None)
                   for window in cast(list[dict[str, str]], clause["windows"])):
            return
    except (KeyError, StopIteration, TypeError, ValueError):
        return
    if (session.scalar(select(Submission.id).where(
            Submission.kind == "owner_editorial_review",
            Submission.payload["raw_item_id"].as_integer() == raw_id,
        ).limit(1)) is not None or session.scalar(select(SourceRevision.id).where(
            SourceRevision.raw_item_id == raw_id,
        ).limit(1)) is not None):
        return
    _set(record, now=now, state="waiting", action="prepare", next_at=now,
         reason="unexpired_road_work_review_reopened", road_work_reopened_at=now.isoformat(),
         attempt_records=[{**_attempt_archive(run, now), "post_id": post.id}])
    record.finished_at = None
    session.delete(run)


def _reissue_unattempted_event_fallback(
    session: Session, record: SchedulerRun, *, now: datetime,
) -> None:
    """Одна новая выдача резерва для будущего события без черновика или попытки GPT."""
    from relocant_media.services.event_place import event_occurrences, resolve_saved_event_place

    data = record.details
    raw_id = data.get("raw_item_id")
    if (type(raw_id) is not int or data.get("lifecycle_root_raw_id")
            or data.get("replacement_raw_item_id") or "fallback_reissued_at" in data
            or data.get("fallback_attempts") != 0 or data.get("attempts") != PRIMARY_ATTEMPTS
            or not _validate({**data, "state": "waiting_fallback", "next_action": "fallback",
                              "next_attempt_at": now.isoformat()})):
        return
    previous_deadline = _stamp(data["fallback_deadline_at"])
    material_deadline = _stamp(data["deadline_at"])
    if not previous_deadline <= now < material_deadline:
        return
    try:
        raw, source, run, post = _objects(session, raw_id)
        if (run is not None or post is not None or not source.enabled or not source.auto_publish
                or source.ownership != "official" or source.high_risk
                or data.get("fingerprints", {}).get(str(raw_id)) != _fingerprint(raw)
                or _fresh(raw, source, now).action != "keep"):
            return
        place = resolve_saved_event_place(
            session, EvidenceDocument.model_validate(raw, from_attributes=True), [raw_id],
            profile=load_city_profile(source.city),
        )
        if place is None:
            return
        start = min(_stamp(slot["starts_at"]) for slot in event_occurrences(place))
    except (ValueError, KeyError, TypeError):
        return
    deadline = min(material_deadline, start, now + FALLBACK_WAIT)
    if deadline <= now or session.scalar(select(Submission.id).where(
        Submission.kind == "owner_editorial_review",
        Submission.payload["raw_item_id"].as_integer() == raw_id,
    ).limit(1)) is not None:
        return
    _set(record, now=now, state="waiting_fallback", action="fallback", next_at=now,
         reason="verified_event_fallback_reissued", fallback_deadline_at=deadline.isoformat(),
         fallback_reissued_at=now.isoformat(),
         previous_fallback_deadline_at=previous_deadline.isoformat())
    record.finished_at = None


def _refresh(session: Session, raw_id: int, now: datetime) -> SchedulerRun:
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if _published(run, post):
        _set(record, now=now, state="published", action=None, reason="telegram_receipt_confirmed")
        return record
    if record.details.get("state") in TERMINAL_STATES:
        return record
    if not _validate(record.details):
        _block(session, record, run, post, now=now, reason="invalid_lifecycle_state")
        return record
    if _unsafe_delivery(run, post):
        _block(session, record, run, post, now=now, reason="delivery_reconciliation_required")
        return record
    if run and run.outcome.get("reason") == SOURCE_REPLACED_REASON:
        target_id = run.outcome.get("replacement_raw_item_id")
        if type(target_id) is int and target_id != raw_id:
            inherit_lifecycle(session, raw_id, target_id, now=now)
            return _refresh(session, target_id, now)
        _block(session, record, run, post, now=now, reason="invalid_source_replacement")
        return record
    if (not source.enabled
            or record.details.get("fingerprints", {}).get(str(raw_id)) != _fingerprint(raw)):
        _block(session, record, run, post, now=now, reason="source_or_evidence_changed")
        return record
    try:
        fresh = _fresh(raw, source, now)
    except ValueError:
        _block(session, record, run, post, now=now, reason="invalid_publication_time")
        return record
    if fresh.action == "discard":
        _block(session, record, run, post, now=now, reason=fresh.reason)
        return record
    if now >= _stamp(record.details["deadline_at"]):
        _block(session, record, run, post, now=now, reason="material_deadline_exceeded")
        return record
    if record.details["state"] == "processing":
        if now >= _stamp(record.details["attempt_deadline_at"]):
            if record.details.get("attempt_kind") in {"delivery", "owner_delivery"}:
                delivery_failure(session, raw_id, "AttemptInterrupted", now=now)
                return record
            return after_error(session, raw_id, "AttemptInterrupted", now=now)
        return record
    if (record.details["state"] == "waiting_fallback"
            and now >= _stamp(record.details["fallback_deadline_at"])):
        _block(session, record, run, post, now=now, reason="fallback_deadline_exceeded")
        return record
    owner_approved = _owner_approved(session, raw_id, run)
    ranking = (run.outcome if run else {}).get("ranking")
    category = str(ranking.get("category", "")).strip().lower() if isinstance(ranking, dict) else ""
    if (not owner_approved and run is not None and post is not None
            and run.outcome.get("status") == post.status == "human_review"
            and _reviewable_utility(source, run, post, run.outcome.get("reason"))):
        _wait_for_owner(record, now=now)
        return record
    if not owner_approved and (source.high_risk or not source.auto_publish
            or category in HIGH_RISK_CATEGORIES
            or category and category not in AUTO_PUBLISH_CATEGORIES):
        _block(session, record, run, post, now=now, reason="separate_publication_approval_required")
        return record
    coverage_reason = _stored_coverage_reason(run)
    if coverage_reason == "independent_sensitivity_review_required":
        if not owner_approved:
            _block(session, record, run, post, now=now,
                   reason="separate_publication_approval_required")
            return record
    elif coverage_reason is not None:
        _block(session, record, run, post, now=now, reason=coverage_reason)
        return record
    if run is None:
        from relocant_media.services.editorial import _reviewed_event_match

        cache = cast(_SweepCache | None, session.info.get("material_lifecycle_cache"))
        match = _reviewed_event_match(session, anchor=raw, anchor_source=source, now=now,
                                     candidates=cache.reviewed_targets if cache else None)
        if match is not None:
            target, proof = match
            reason = ("related_delivery_reconciliation_required"
                      if proof.kind == "delivery_reconciliation" else "event_duplicate_confirmed")
            record.details = {**record.details, "rejection_review": {
                "policy_version": "event-duplicate-review-v1", "kind": "deterministic_event",
                "raw_item_id": raw_id, "duplicate_of_raw_item": target.id,
                "decision": ("delivery_reconciliation" if proof.kind == "delivery_reconciliation"
                             else "confirm_rejection"),
                "basis": proof.kind, "context_sha256": proof.context_sha256,
                "reviewed_at": now.isoformat()}}
            _block(session, record, run, post, now=now, reason=reason)
            return record
        if fresh.action == "hold" and record.details["next_action"] == "prepare":
            _set(record, now=now, state="waiting", action="retry_prepare",
                 reason=fresh.reason, next_at=now+RETRY_DELAY)
        return record
    outcome = run.outcome
    if owner_approved:
        if record.details["next_action"] != "owner_delivery":
            _set(record, now=now, state="waiting", action="owner_delivery",
                 reason="explicit_owner_approval", next_at=now)
    elif outcome.get("status") == "ready":
        if record.details["next_action"] != "deliver":
            _set(record, now=now, state="ready", action="deliver",
                 reason="reviewed_ready", next_at=now)
    elif record.details["next_action"] in {"retry_prepare", "repair", "fallback"}:
        return record
    elif outcome.get("status") in {"skipped", "blocked"}:
        if (record.details.get("attempt_kind") == "fallback"
                and record.details["fallback_attempts"] == FALLBACK_ATTEMPTS):
            _block(session, record, run, post, now=now, reason="fallback_editorial_rejection")
        elif post is None and record.details["next_action"] == "audit_rejection":
            return record
        elif post is None and record.details["audit_attempts"] < AUDIT_ATTEMPTS:
            _set(record, now=now, state="waiting", action="audit_rejection",
                 reason="rejection_requires_audit", next_at=now)
        elif post is None:
            _unconfirmed_rejection_fallback(session, record, run, post, now)
        else:
            _block(session, record, run, post, now=now, reason="repeated_editorial_rejection")
    elif outcome.get("status") == "human_review":
        if record.details["next_action"] not in {"repair", "fallback"}:
            if record.details["attempts"] < PRIMARY_ATTEMPTS:
                _set(record, now=now, state="waiting", action="repair",
                     reason=str(outcome.get("reason", "technical_review_hold")),
                     next_at=now+RETRY_DELAY)
            else:
                _fallback_or_block(session, record, run, post, now)
    else:
        _block(session, record, run, post, now=now, reason="unknown_editorial_state")
    return record


def lifecycle_state(session: Session, raw_id: int, *, now: datetime) -> dict[str, Any]:
    return dict(_refresh(session, raw_id, _now(now)).details)


def _fallback_or_block(session: Session, record: SchedulerRun, run: EditorialRun | None,
                       post: Post | None, now: datetime, *,
                       reason: str = "primary_attempt_budget_exhausted") -> None:
    if record.details["fallback_attempts"] >= FALLBACK_ATTEMPTS:
        _block(session, record, run, post, now=now, reason="attempt_budget_exhausted")
    else:
        deadline = min(_stamp(record.details["deadline_at"]), now+FALLBACK_WAIT)
        if record.details.get("fallback_deadline_at"):
            deadline = min(deadline, _stamp(record.details["fallback_deadline_at"]))
        _set(record, now=now, state="waiting_fallback", action="fallback",
             reason=reason, next_at=now,
             fallback_deadline_at=deadline.isoformat())


def _unconfirmed_rejection_fallback(session: Session, record: SchedulerRun,
                                   run: EditorialRun | None, post: Post | None,
                                   now: datetime) -> None:
    # Допуск только к резерву: источник, риск, Post и квитанции проверяются отдельно.
    record.details = {**record.details, "rejection_justified": None,
                      "unconfirmed_rejection_fallback": True}
    _fallback_or_block(session, record, run, post, now,
                       reason="rejection_audit_budget_exhausted")


def before_attempt(session: Session, raw_id: int, *, now: datetime,
                   channel: Literal["primary", "fallback", "rejection_audit"] = "primary") -> bool:
    now = _now(now)
    record = _refresh(session, raw_id, now)
    data = record.details
    if data["state"] in TERMINAL_STATES or data["state"] == "processing":
        return False
    early_fallback = (channel == "fallback" and data["attempts"] >= 1
                      and bool(data.get("last_error_type"))
                      and data["next_action"] in {"retry_prepare", "repair", "fallback"})
    if (not data.get("next_attempt_at")
            or now < _stamp(data["next_attempt_at"]) and not early_fallback):
        return False
    allowed = {"primary": {"prepare", "retry_prepare", "repair"}, "fallback": {"fallback"},
               "rejection_audit": {"audit_rejection"}}[channel]
    if data["next_action"] not in allowed and not early_fallback:
        return False
    raw, source, run, post = _objects(session, raw_id)
    if _fresh(raw, source, now).action != "keep":
        _block(session, record, run, post, now=now, reason="source_freshness_not_confirmed")
        return False
    counter, maximum = {"primary": ("attempts", PRIMARY_ATTEMPTS),
                        "fallback": ("fallback_attempts", FALLBACK_ATTEMPTS),
                        "rejection_audit": ("audit_attempts", AUDIT_ATTEMPTS)}[channel]
    if channel == "fallback" and data.get("reason") == FLIGHT_REVIEW_REASON:
        counter, maximum = "flight_review_attempts", 1
    if data[counter] >= maximum:
        if channel == "rejection_audit":
            _unconfirmed_rejection_fallback(session, record, run, post, now)
        else:
            _fallback_or_block(session, record, run, post, now)
        return False
    _set(record, now=now, state="processing", action="process", reason="attempt_started",
         next_at=min(_stamp(data["deadline_at"]), now+ATTEMPT_LEASE),
         attempt_deadline_at=min(_stamp(data["deadline_at"]), now+ATTEMPT_LEASE).isoformat(),
         attempt_kind=channel, retry_authorized=(data["next_action"] == "retry_prepare"
             or channel == "fallback" and (data.get("rejection_justified") is False
                 or data.get("unconfirmed_rejection_fallback") is True)),
         **{counter: data[counter]+1})
    return True


def after_result(session: Session, raw_id: int, *, now: datetime) -> dict[str, Any]:
    now = _now(now)
    raw, source, _, _ = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if record.details.get("state") == "processing":
        _set(record, now=now, state="waiting", action="prepare",
             reason="attempt_completed", next_at=now, retry_authorized=False)
    return dict(_refresh(session, raw_id, now).details)


def before_delivery_attempt(session: Session, raw_id: int, *, now: datetime,
                            channel: Literal["primary", "owner"] = "primary") -> bool:
    """Зафиксировать отдельную попытку доставки до live fetch и внешнего вызова."""
    now = _now(now)
    record = _refresh(session, raw_id, now)
    data = record.details
    expected_state, expected_action = (("waiting", "owner_delivery") if channel == "owner"
                                       else ("ready", "deliver"))
    if (data.get("state") != expected_state or data.get("next_action") != expected_action
            or now < _stamp(data["next_attempt_at"])):
        return False
    _, _, run, post = _objects(session, raw_id)
    if data["delivery_attempts"] >= DELIVERY_ATTEMPTS:
        _block(session, record, run, post, now=now, reason="delivery_attempt_budget_exhausted")
        return False
    deadline = min(_stamp(data["deadline_at"]), now+ATTEMPT_LEASE)
    _set(record, now=now, state="processing", action="process", reason="delivery_attempt_started",
         next_at=deadline, attempt_deadline_at=deadline.isoformat(),
         attempt_kind="owner_delivery" if channel == "owner" else "delivery",
         delivery_attempts=data["delivery_attempts"]+1, retry_authorized=False)
    return True


def before_owner_delivery_attempt(session: Session, raw_id: int, *, now: datetime,
                                   validated_legacy_review_id: int | None = None) -> bool:
    if validated_legacy_review_id is not None:
        # Caller уже проверил _legacy_precision_block и exact package/draft.
        raw, source, run, _ = _objects(session, raw_id)
        review = session.get(Submission, validated_legacy_review_id)
        if (run is None or review is None or review.kind != "owner_editorial_review"
                or review.status != "publish_blocked"
                or review.payload.get("owner_decision") != "publish"
                or review.payload.get("raw_item_id") != raw_id
                or review.payload.get("package_sha256") != run.outcome.get("_package_sha256")
                or not isinstance(review.payload.get("draft_sha256"), str)
                or len(review.payload["draft_sha256"]) != 64
                or any(key in review.payload for key in ("publish_block_reason",
                    "publish_claimed_at", "publish_recovered_at", "telegram_channel_message_id"))):
            return False
        record = _record(session, raw, source, _now(now))
        if record.details.get("state") in TERMINAL_STATES:
            return False
        record.details = {**record.details, "validated_legacy_review_id": review.id}
    return before_delivery_attempt(session, raw_id, now=now, channel="owner")


def delivery_failure(session: Session, raw_id: int, error_type: str, *,
                     now: datetime) -> dict[str, Any]:
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    data = record.details
    owner_approved = _owner_approved(session, raw_id, run)
    if data.get("state") in TERMINAL_STATES:
        return dict(data)
    if _published(run, post):
        _set(record, now=now, state="published", action=None, reason="telegram_receipt_confirmed")
    elif _unsafe_delivery(run, post):
        _block(session, record, run, post, now=now, reason="delivery_reconciliation_required")
    elif now >= _stamp(data["deadline_at"]):
        _block(session, record, run, post, now=now, reason="material_deadline_exceeded")
    elif data["delivery_attempts"] >= DELIVERY_ATTEMPTS:
        _block(session, record, run, post, now=now, reason="delivery_attempt_budget_exhausted")
    elif run is None or run.outcome.get("status") != "ready" and not owner_approved:
        _block(session, record, run, post, now=now, reason="delivery_result_not_ready")
    else:
        kind = (error_type if error_type.isidentifier() and len(error_type) <= 80
                else "DeliveryError")
        _set(record, now=now, state="waiting" if owner_approved else "ready",
             action="owner_delivery" if owner_approved else "deliver", reason="delivery_error",
             next_at=min(_stamp(data["deadline_at"]), now+RETRY_DELAY), last_error_type=kind,
             retry_authorized=False)
    return dict(record.details)


def request_flight_review(
    session: Session, raw_id: int, *, parent_raw_id: int, parent_message_id: int, now: datetime,
) -> str:
    """Записать решение до отправки и передать его существующей резервной очереди."""
    from relocant_media.services.flight_review import flight_review_reference

    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    data = record.details
    if (run is None or post is None or run.delivery_status != "pending"
            or run.outcome.get("status") != "ready" or _has_receipt(run, post)
            or data.get("state") != "processing" or data.get("attempt_kind") != "delivery"):
        raise LifecycleBlockedError("Нет безопасно удержанной попытки доставки")
    parent = {"raw_item_id": parent_raw_id, "message_id": parent_message_id}
    run.outcome = {**run.outcome, "status": "human_review", "reason": FLIGHT_REVIEW_REASON,
                   "_flight_review": parent}
    post.status = "human_review"
    record.details = {**data, "delivery_attempts": data["delivery_attempts"] - 1,
                      "flight_review": parent}
    try:
        reference = flight_review_reference(session, run, city=source.city, now=now)
    except ValueError:
        reference = None
    deadline = _stamp(data["deadline_at"])
    if reference is None or now >= deadline or data.get("flight_review_attempts", 0):
        _block(session, record, run, post, now=now, reason="flight_linked_review_unavailable")
    else:
        _set(record, now=now, state="waiting_fallback", action="fallback",
             reason=FLIGHT_REVIEW_REASON, next_at=now, flight_review_attempts=0,
             fallback_deadline_at=min(deadline, now + FALLBACK_WAIT).isoformat())
    return str(record.details["reason"])


def confirm_flight_duplicate(session: Session, raw_id: int, *, now: datetime) -> None:
    from relocant_media.services.flight_review import flight_review_reference

    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if (record.details.get("state") != "processing"
            or record.details.get("attempt_kind") != "fallback"
            or record.details.get("flight_review_attempts") != 1
            or flight_review_reference(session, run, city=source.city, now=now) is None):
        raise LifecycleBlockedError("Материал не ожидает решения о повторе авиановости")
    _block(session, record, run, post, now=now, reason="flight_duplicate_confirmed")


delivery_failed = delivery_failure


def adopt_fallback_request(session: Session, raw_id: int, *, now: datetime) -> bool:
    """Вызывается только после штатного validate_fallback_candidate, без доверия к payload."""
    now = _now(now)
    record = _refresh(session, raw_id, now)
    data = record.details
    if (data.get("state") in TERMINAL_STATES or data.get("state") == "processing"
            or data.get("reason") == FLIGHT_REVIEW_REASON
            or data.get("next_action") not in {"prepare", "retry_prepare", "repair", "fallback"}
            or data["fallback_attempts"] >= FALLBACK_ATTEMPTS):
        return False
    deadline = min(_stamp(data["deadline_at"]), now+FALLBACK_WAIT)
    if data.get("fallback_deadline_at"):
        deadline = min(deadline, _stamp(data["fallback_deadline_at"]))
    _set(record, now=now, state="waiting_fallback", action="fallback",
         reason="validated_existing_fallback_request", next_at=now,
         attempts=max(1, data["attempts"]), last_error_type="ValidatedFallbackRequest",
         fallback_deadline_at=deadline.isoformat())
    return True


def record_rejection_review(session: Session, raw_id: int, review: dict[str, Any], *,
                            now: datetime) -> None:
    raw, source, _, _ = _objects(session, raw_id)
    record = _record(session, raw, source, _now(now))
    if record.details.get("state") in TERMINAL_STATES:
        return
    history = list(record.details.get("rejection_review_history", []))
    previous = record.details.get("rejection_review")
    if not history and isinstance(previous, dict):
        history.append(copy.deepcopy(previous))
    history.append(copy.deepcopy(review))
    record.details = {**record.details, "rejection_review": copy.deepcopy(review),
                      "rejection_review_history": history[-AUDIT_ATTEMPTS:]}


def block_unsupported_repair(session: Session, raw_id: int, *, now: datetime) -> dict[str, Any]:
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if record.details.get("state") not in TERMINAL_STATES:
        _block(session, record, run, post, now=now, reason="unsupported_repair")
    return dict(record.details)


def confirm_transport_duplicate(
    session: Session, raw_id: int, previous_post_id: int, *, now: datetime,
) -> bool:
    """Зафиксировать повтор под уже удерживаемой блокировкой города."""
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    previous = session.get(Post, previous_post_id)
    if (run is None or run.delivery_status != "pending" or _unsafe_delivery(run, post)
            or _owner_approved(session, raw_id, run)
            or previous is None or previous.city != source.city
            or not previous.telegram_message_id):
        return False
    record = _record(session, raw, source, now)
    _block(session, record, run, post, now=now, reason="published_transport_duplicate")
    record.details = {**record.details, "duplicate_of_post_id": previous_post_id}
    return True


def close_by_owner(session: Session, raw_id: int, *, now: datetime) -> dict[str, Any]:
    """Закрыть материал по явному решению владельца; commit делает канонический caller.

    Квитанции, транспорт и подтверждённое согласование не переписываются. Сверка
    неизвестной доставки остаётся отдельной причиной окончательной блокировки.
    """
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if record.details.get("state") == "blocked":
        return dict(record.details)
    if _published(run, post):
        if record.details.get("state") != "published":
            _set(record, now=now, state="published", action=None,
                 reason="telegram_receipt_confirmed")
        return dict(record.details)
    reason = ("delivery_reconciliation_required" if _unsafe_delivery(run, post)
              or record.details.get("state") == "published" else "owner_closed_backlog")
    history = list(record.details.get("history", []))
    previous = {"previous_state": record.details.get("state"),
                "previous_reason": record.details.get("reason")}
    _block(session, record, run, post, now=now, reason=reason)
    # Разовая ручная отмена сохраняет всю прежнюю историю, включая старейшую запись.
    record.details = {**record.details, "history": [*history, {
        "at": now.isoformat(), "state": "blocked", "next_action": None, "reason": reason,
    }], "owner_closure": {"at": now.isoformat(), "reason": "owner_closed_backlog", **previous}}
    return dict(record.details)


def valid_owner_close_state(data: dict[str, Any]) -> bool:
    """Без записи проверить нетерминальный журнал перед атомарной ручной отменой."""
    return _validate(data)


def authorize_refreshed_evidence(session: Session, raw_id: int, *, now: datetime) -> bool:
    """Принять уже валидированный capture без нового бюджета, пакета и разрешения доставки."""
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    data = record.details
    if (data.get("state") in TERMINAL_STATES or not _validate(data)
            or now >= _stamp(data["deadline_at"]) or not source.enabled
            or post is not None or run and (run.post_id is not None
                or run.delivery_status != "pending" or _has_receipt(run, post)
                or any(key in run.outcome for key in (
                    "_package", "_owner_review", "_reader_submission_id", "_rich_delivery",
                    "_media_delivery",
                )))):
        return False
    old = data.get("fingerprints", {}).get(str(raw_id))
    history = list(data.get("evidence_refreshes", []))
    history.append({"at": now.isoformat(), "raw_item_id": raw_id, "previous_sha256": old})
    record.details = {**data, "fingerprints": {**data.get("fingerprints", {}),
                      str(raw_id): _fingerprint(raw)}, "evidence_refreshes": history[-MAX_HISTORY:]}
    return True


def after_error(session: Session, raw_id: int, error_type: str, *, now: datetime,
                channel: Literal["primary", "fallback", "rejection_audit"] | None = None,
                ) -> SchedulerRun:
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if record.details.get("state") in TERMINAL_STATES:
        return record
    data = record.details
    kind = error_type if error_type.isidentifier() and len(error_type) <= 80 else "ProcessingError"
    attempt_kind = channel or data.get("attempt_kind", "primary")
    record.details = {**data, "last_error_type": kind}
    if _unsafe_delivery(run, post):
        _block(session, record, run, post, now=now, reason="delivery_reconciliation_required")
    elif now >= _stamp(data["deadline_at"]):
        _block(session, record, run, post, now=now, reason="material_deadline_exceeded")
    elif attempt_kind == "rejection_audit":
        history = list(record.details.get("rejection_review_history", []))
        previous = record.details.get("rejection_review")
        if not history and isinstance(previous, dict):
            history.append(copy.deepcopy(previous))
        history.append({"decision": "audit_error", "error_type": kind,
                        "reviewed_at": now.isoformat(), "audit_attempt": data["audit_attempts"]})
        record.details = {**record.details, "rejection_review_history": history[-AUDIT_ATTEMPTS:]}
        if data["audit_attempts"] >= AUDIT_ATTEMPTS:
            _unconfirmed_rejection_fallback(session, record, run, post, now)
        else:
            _set(record, now=now, state="waiting", action="audit_rejection",
                 reason="rejection_audit_error",
                 next_at=min(_stamp(data["deadline_at"]), now+RETRY_DELAY),
                 retry_authorized=False)
    elif attempt_kind == "fallback":
        _block(session, record, run, post, now=now, reason="fallback_attempt_failed")
    elif data["attempts"] >= PRIMARY_ATTEMPTS:
        _fallback_or_block(session, record, run, post, now)
    else:
        _set(record, now=now, state="waiting", action="repair" if post else "retry_prepare",
             reason="processing_error", next_at=min(_stamp(data["deadline_at"]), now+RETRY_DELAY),
             last_error_type=kind, retry_authorized=False)
    return record


def rejection_reviewed(session: Session, raw_id: int, *, justified: bool,
                       now: datetime,
                       confirmed_reason: Literal["rejection_confirmed", "event_duplicate_confirmed"]
                       = "rejection_confirmed") -> dict[str, Any]:
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if (record.details.get("state") == "processing"
            and record.details.get("attempt_kind") == "rejection_audit"):
        _set(record, now=now, state="waiting", action="audit_rejection",
             reason="rejection_audit_completed", next_at=now)
    record = _refresh(session, raw_id, now)
    if record.details.get("state") in TERMINAL_STATES:
        return dict(record.details)
    if (record.details.get("next_action") != "audit_rejection"
            or record.details["rejection_audits"] >= AUDIT_ATTEMPTS or run is None
            or run.outcome.get("status") not in {"skipped", "blocked"}):
        raise LifecycleBlockedError("Отказ не ожидает проверки")
    record.details = {**record.details,
                      "rejection_audits": record.details["rejection_audits"]+1,
                      "rejection_justified": justified}
    if justified:
        _block(session, record, run, post, now=now, reason=confirmed_reason)
    elif (record.details["attempts"] < PRIMARY_ATTEMPTS
            and record.details["audit_attempts"] < AUDIT_ATTEMPTS):
        _set(record, now=now, state="waiting", action="retry_prepare",
             reason="rejection_not_justified", next_at=now)
    else:
        _fallback_or_block(session, record, run, post, now, reason="rejection_not_justified")
    return dict(record.details)


def block_related_delivery(session: Session, raw_id: int, *, now: datetime) -> dict[str, Any]:
    """Неопределённая отправка того же события не разрешает новую отправку другой raw."""
    now = _now(now)
    raw, source, run, post = _objects(session, raw_id)
    record = _record(session, raw, source, now)
    if record.details.get("state") in TERMINAL_STATES:
        return dict(record.details)
    if (record.details.get("state") != "processing"
            or record.details.get("attempt_kind") != "rejection_audit"):
        raise LifecycleBlockedError("Сверка дубля не владеет попыткой проверки")
    record.details = {**record.details,
                      "rejection_audits": record.details["rejection_audits"]+1}
    _block(session, record, run, post, now=now, reason="related_delivery_reconciliation_required")
    return dict(record.details)


def authorize_retry(session: Session, raw_id: int, *, now: datetime) -> bool:
    now = _now(now)
    record = _refresh(session, raw_id, now)
    return (record.details.get("state") not in TERMINAL_STATES
            and can_retry_snapshot(session, raw_id, now=now))


def can_retry_snapshot(session: Session, raw_id: int, *, now: datetime) -> bool:
    """Read-only допуск повторной подготовки; новый отказ или ручной пакет не обходит."""
    now = _now(now)
    if is_terminal_blocked(session, raw_id, now=now):
        return False
    record = _read_record(session, raw_id)
    if record is None:
        return False
    raw, source, run, post = _objects(session, raw_id)
    data = record.details
    ranking = run.outcome.get("ranking") if run else None
    category = str(ranking.get("category", "")).strip().lower() if isinstance(ranking, dict) else ""
    allowed = (data.get("next_action") == "retry_prepare"
               and now >= _stamp(data["next_attempt_at"])
               or data.get("state") == "processing" and data.get("retry_authorized") is True
               or data.get("next_action") == "fallback"
               and (data.get("rejection_justified") is False
                    or data.get("unconfirmed_rejection_fallback") is True))
    try:
        return bool(allowed and source.enabled and source.auto_publish and not source.high_risk
                    and (not category or category in AUTO_PUBLISH_CATEGORIES)
                    and not _owner_approved(session, raw_id, run)
                    and run is not None and post is None and run.post_id is None
                    and run.delivery_status == "pending" and not _has_receipt(run, post)
                    and not any(key in run.outcome for key in (
                        "_package", "_owner_review", "_reader_submission_id", "_rich_delivery",
                        "_media_delivery",
                    )) and data.get("fingerprints", {}).get(str(raw_id)) == _fingerprint(raw)
                    and _fresh(raw, source, now).action == "keep")
    except ValueError:
        return False


def _attempt_archive(run: EditorialRun, now: datetime) -> dict[str, Any]:
    return {"outcome": copy.deepcopy(run.outcome), "evidence": copy.deepcopy(run.evidence),
            "created_at": run.created_at.isoformat(),
            "editorial_rule_version": run.editorial_rule_version, "archived_at": now.isoformat()}


def archive_for_retry(session: Session, raw_id: int, *, now: datetime) -> bool:
    """Одноразово убрать безопасный run из обычного отбора, сохранив точную старую запись."""
    now = _now(now)
    if not authorize_retry(session, raw_id, now=now):
        return False
    raw, source, run, _ = _objects(session, raw_id)
    assert run is not None
    record = _record(session, raw, source, now)
    archive = list(record.details.get("attempt_records", []))
    if len(archive) >= PRIMARY_ATTEMPTS:
        return False
    archive.append(_attempt_archive(run, now))
    record.details = {**record.details, "attempt_records": archive, "retry_authorized": False}
    session.delete(run)
    session.flush()
    return True


def delivery_deadline(session: Session, raw_id: int) -> datetime | None:
    """Read-only срок жизни материала; None, если записи нет или срок не читается."""
    record = _read_record(session, raw_id)
    try:
        return _stamp(record.details["deadline_at"]) if record is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def hold_until_morning(session: Session, raw_id: int, *, now: datetime,
                       deadline: datetime) -> bool:
    """Продлить срок жизни готового несрочного материала до утреннего выпуска (#359).

    Только для проверенного материала в состоянии ready/deliver и только вперёд.
    Свежесть, источник и ревью перепроверяются в обычной доставке утром.
    """
    now = _now(now)
    record = _refresh(session, raw_id, now)
    data = record.details
    if data.get("state") != "ready" or data.get("next_action") != "deliver":
        return False
    if _stamp(data["deadline_at"]) < deadline:
        record.details = {**data, "deadline_at": deadline.isoformat(),
                          "morning_hold": {"at": now.isoformat(),
                                           "previous_deadline_at": data["deadline_at"]}}
    return True


def _read_record(session: Session, raw_id: int) -> SchedulerRun | None:
    raw = session.get(RawItem, raw_id)
    source = session.get(Source, raw.source_id) if raw else None
    if raw is None or source is None:
        return None
    record = _find_record(session, source.city, raw_id)
    if record is not None and type(record.details.get("lifecycle_root_raw_id")) is int:
        return _find_record(session, source.city, record.details["lifecycle_root_raw_id"]) or record
    return record


def is_terminal_blocked(session: Session, raw_id: int, *, now: datetime | None = None) -> bool:
    """Проверить закрытие без записи, в том числе в read-only preview."""
    if session.get(RawItem, raw_id) is None:
        return True
    record = _read_record(session, raw_id)
    if record is None:
        return False
    data = record.details
    if data.get("state") in TERMINAL_STATES:
        return True
    if not _validate(data):
        return True
    if now is not None:
        current = _now(now)
        if current >= _stamp(data["deadline_at"]):
            return True
        if (data.get("state") == "waiting_fallback"
                and current >= _stamp(data["fallback_deadline_at"])):
            return True
    return False


def response_deadline(session: Session, raw_id: int) -> datetime | None:
    """Крайний срок резервного ответа без записи; None, если цикл ещё не открыт."""
    record = _read_record(session, raw_id)
    if record is None or not _validate(record.details):
        return None
    data = record.details
    deadline = _stamp(data["deadline_at"])
    if data.get("fallback_deadline_at"):
        deadline = min(deadline, _stamp(data["fallback_deadline_at"]))
    return deadline


def fallback_reissued_at(session: Session, raw_id: int) -> datetime | None:
    record = _read_record(session, raw_id)
    marker = record.details.get("fallback_reissued_at") if record is not None else None
    return _stamp(marker) if marker is not None else None


def ensure_not_terminal(session: Session, raw_id: int, *, now: datetime | None = None) -> None:
    if is_terminal_blocked(session, raw_id, now=now):
        raise LifecycleBlockedError("Материал окончательно заблокирован жизненным циклом")


def inherit_lifecycle(session: Session, original_id: int, replacement_id: int, *,
                      now: datetime) -> None:
    """Связать официальный первоисточник с исходным общим бюджетом, без нового окна."""
    now = _now(now)
    original, source, _, _ = _objects(session, original_id)
    replacement, target_source, _, _ = _objects(session, replacement_id)
    root = _record(session, original, source, now)
    if (original_id == replacement_id or source.city != target_source.city
            or root.details.get("replacement_raw_item_id") not in {None, replacement_id}
            or replacement_id == root.details.get("raw_item_id")):
        raise LifecycleBlockedError("Недопустимая цепочка замены первоисточника")
    alias = _find_record(session, target_source.city, replacement_id)
    if alias is not None:
        if alias.details.get("lifecycle_root_raw_id") == root.details["raw_item_id"]:
            return
        raise LifecycleBlockedError("Первоисточник уже имеет независимый жизненный цикл")
    fingerprints = {**root.details.get("fingerprints", {}),
                    str(replacement_id): _fingerprint(replacement)}
    root.details = {**root.details, "fingerprints": fingerprints,
                    "replacement_raw_item_id": replacement_id}
    session.add(SchedulerRun(
        task_key=_key(target_source.city, replacement_id), task_type=TASK_TYPE,
        city=target_source.city, scheduled_for=now, started_at=now, attempt=1, status="linked",
        details={"lifecycle_root_raw_id": root.details["raw_item_id"],
                 "raw_item_id": replacement_id, "policy_version": POLICY_VERSION},
    ))
    session.flush()


def sweep_materials(session: Session, *, city: str, now: datetime,
                    raw_ids: set[int] | None = None,
                    reissue_event_fallback: bool = False) -> LifecycleSweep:
    now = _now(now)
    query = (select(RawItem, Source, EditorialRun, Post).join(Source)
             .outerjoin(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
             .outerjoin(Post, Post.id == EditorialRun.post_id).where(Source.city == city))
    if raw_ids is not None:
        query = query.where(RawItem.id.in_(raw_ids))
    records = {row.task_key: row for row in session.scalars(select(SchedulerRun).where(
        SchedulerRun.city == city, SchedulerRun.task_type == TASK_TYPE,
    ).order_by(SchedulerRun.id))}
    for record in records.values():
        if (record.status != "blocked"
                or raw_ids is not None and record.details.get("raw_item_id") not in raw_ids):
            continue
        if record.details.get("reason") == "separate_publication_approval_required":
            _restore_unqueued_utility(session, record, now=now)
        elif record.details.get("reason") == "draft_coverage_ungrounded":
            _reopen_unexpired_road_work(session, record, now=now)
        elif (reissue_event_fallback
                and record.details.get("reason") == "fallback_deadline_exceeded"):
            _reissue_unattempted_event_fallback(session, record, now=now)
    session.flush()
    closed_ids = {row.details["raw_item_id"] for row in records.values()
                  if row.details.get("state") in TERMINAL_STATES
                  and type(row.details.get("raw_item_id")) is int}
    closed_ids.update(row.details["raw_item_id"] for row in records.values()
                      if row.details.get("lifecycle_root_raw_id") in closed_ids
                      and type(row.details.get("raw_item_id")) is int)
    terminal_raw_ids = select(SchedulerRun.details["raw_item_id"].as_integer()).where(
        SchedulerRun.city == city, SchedulerRun.task_type == TASK_TYPE,
        SchedulerRun.status.in_(TERMINAL_STATES),
    )
    query = query.where(RawItem.id.not_in(terminal_raw_ids))
    # Один bulk read вместо отдельных ORM/ledger-запросов на каждый материал.
    objects: dict[int, tuple[RawItem, Source, EditorialRun | None, Post | None]] = {
        row[0].id: (row[0], row[1], row[2], row[3])
        for row in session.execute(query.order_by(RawItem.id))}
    reviews = _owner_reviews(session)
    from relocant_media.services.editorial import _reviewed_event_candidates

    cache = _SweepCache(objects, records, reviews, _reviewed_event_candidates(session, city))
    selected_closed = closed_ids if raw_ids is None else closed_ids & raw_ids
    due, audits, fallback, excluded = [], [], [], set(selected_closed)
    blocked = [raw_id for raw_id in selected_closed
               if _read_record_from_cache(cache, city, raw_id).details.get("state") == "blocked"]
    session.info["material_lifecycle_cache"] = cache
    try:
        for raw_id, (_, _, run, post) in objects.items():
            if raw_id in closed_ids:
                continue
            if _published(run, post):
                excluded.add(raw_id)
                continue
            record = _refresh(session, raw_id, now)
            data = record.details
            if data["state"] in TERMINAL_STATES:
                excluded.add(raw_id)
                if data["state"] == "blocked":
                    blocked.append(raw_id)
                continue
            if (run and run.outcome.get("reason") == SOURCE_REPLACED_REASON
                    or not data.get("next_attempt_at")
                    or now < _stamp(data["next_attempt_at"])):
                excluded.add(raw_id)
                continue
            if data["next_action"] == "audit_rejection":
                audits.append(raw_id)
                excluded.add(raw_id)
            elif data["next_action"] == "fallback":
                fallback.append(raw_id)
                excluded.add(raw_id)
            elif data["next_action"] in {"prepare", "retry_prepare", "repair"}:
                due.append(raw_id)
            else:
                excluded.add(raw_id)
    finally:
        session.info.pop("material_lifecycle_cache", None)
    session.flush()
    return LifecycleSweep(tuple(due), tuple(audits), tuple(fallback),
                          frozenset(excluded), tuple(blocked))


def _read_record_from_cache(cache: _SweepCache, city: str, raw_id: int) -> SchedulerRun:
    record = cache.records[_key(city, raw_id)]
    root_id = record.details.get("lifecycle_root_raw_id")
    if type(root_id) is int:
        return cache.records[_key(city, root_id)]
    return record
