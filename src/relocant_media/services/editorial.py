"""Persisted editorial processing and conservative, at-most-once staging delivery."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.publisher import Publisher
from relocant_media.domain.dedup import (
    DEFAULT_EVENT_WINDOW,
    EventCandidate,
    canonicalize_url,
    event_key,
    normalize_text,
    same_event,
    same_verified_route_detour,
    title_similarity,
)
from relocant_media.domain.event_facts import conflicting_facts
from relocant_media.domain.originality import copies_source_text
from relocant_media.domain.policy import (
    CURRENT_EDITORIAL_RULE_VERSION,
    SOURCE_REPLACED_REASON,
    primary_evidence_kind,
)
from relocant_media.storage.locks import lock_editorial_city as _lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


class DeliveryBlockedError(ValueError):
    pass


class PublishedTransportDuplicate(DeliveryBlockedError):
    def __init__(self, post_id: int) -> None:
        super().__init__(f"transport story repeats published post {post_id}")
        self.post_id = post_id


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


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


def _document(item: RawItem) -> EvidenceDocument:
    return EvidenceDocument(
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        body=item.body,
        published_at=item.published_at,
        published_at_precision=item.published_at_precision,
    )


def source_replacement_target(session: Session, raw_id: int) -> int | None:
    run = session.get(EditorialRun, raw_id)
    if run is None or run.outcome.get("reason") != SOURCE_REPLACED_REASON:
        return None
    target = run.outcome.get("replacement_raw_item_id")
    return target if type(target) is int and target > 0 and target != raw_id else None


@dataclass(frozen=True)
class ReviewedEventProof:
    """Только основание дедупа; не разрешение публикации и не новый редакционный пакет."""

    target_raw_id: int
    kind: Literal["reviewed", "published", "delivery_reconciliation"]
    context_sha256: str


def _document_candidate(document: EvidenceDocument, raw_id: int) -> EventCandidate:
    # Хеш и URL вычисляются из снимка; старые materialized поля RawItem не доказательство.
    return EventCandidate(raw_item_id=raw_id, source_id=document.source_id, url=document.url,
        title=document.title, body=document.body, published_at=document.published_at)


def _same_proven_event(left: EvidenceDocument, right: EvidenceDocument) -> bool:
    left_candidate, right_candidate = _document_candidate(left, 1), _document_candidate(right, 2)
    if same_verified_route_detour(left_candidate, right_candidate):
        return True
    left_body, right_body = normalize_text(left.body), normalize_text(right.body)
    if min(len(left_body.split()), len(right_body.split())) < 4:
        return False
    return bool(same_event(left_candidate, right_candidate)
        and not conflicting_facts(
            f"{left.title}\n{left.body}", f"{right.title}\n{right.body}", 0, 0)
        and (left_body == right_body or title_similarity(left.body, right.body) >= 0.9
             or min(len(left_body), len(right_body)) >= 80
             and title_similarity(left.body, right.body) >= 0.8))


def _stored_duplicate_documents(run: EditorialRun) -> tuple[tuple[int, EvidenceDocument,
                                                                  dict[str, Any]], ...]:
    snapshots = run.evidence.get("documents")
    if snapshots is None:
        snapshots = [{**run.evidence, "raw_item_id": run.raw_item_id}]
    if not isinstance(snapshots, list) or not 1 <= len(snapshots) <= 10:
        raise ValueError("Нет ограниченного снимка проверенного события")
    documents, seen = [], set()
    for snapshot in snapshots:
        if (not isinstance(snapshot, dict) or type(snapshot.get("raw_item_id")) is not int
                or snapshot["raw_item_id"] <= 0 or snapshot["raw_item_id"] in seen):
            raise ValueError("Неверная связь документа с событием")
        document = EvidenceDocument.model_validate(snapshot)
        if (document.published_at is not None and (document.published_at.tzinfo is None
                or document.published_at.utcoffset() is None)):
            raise ValueError("Неизвестен часовой пояс снимка события")
        documents.append((snapshot["raw_item_id"], document, snapshot))
        seen.add(snapshot["raw_item_id"])
    if sum(len(doc.title)+len(doc.body) for _, doc, _ in documents) > 120_000:
        raise ValueError("Снимок проверенного события превышает предел")
    primary = EvidenceDocument.model_validate(run.evidence)
    if not any(raw_id == run.raw_item_id and document == primary
               for raw_id, document, _ in documents):
        raise ValueError("Нет исходного проверенного документа")
    return tuple(documents)


def _possible_saved_package_match(run: EditorialRun, anchor: EvidenceDocument, city: str) -> bool:
    """Только резерв для сверки transport, без восстановления evidence или нового допуска."""
    payload = run.outcome.get("_package")
    expected = run.outcome.get("_package_sha256")
    if not isinstance(payload, dict) or not isinstance(expected, str) or len(expected) != 64:
        return False
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()
        if len(encoded) > 512*1024 or hashlib.sha256(encoded).hexdigest() != expected:
            return False
        from relocant_media.ai.package import EditorialPackage

        package = EditorialPackage.model_validate_json(encoded)
        if package.raw_item_id != run.raw_item_id or package.city != city:
            return False
        saved = package.event.documents if package.event is not None else (package.evidence,)
        if not 1 <= len(saved) <= 10:
            return False
        documents = [EvidenceDocument.model_validate(document.model_dump(
            include=set(EvidenceDocument.model_fields))) for document in saved]
        if sum(len(doc.title)+len(doc.body) for doc in documents) > 120_000:
            return False
        return any(same_event(_document_candidate(anchor, 1), _document_candidate(doc, 2))
                   for doc in documents)
    except (ValueError, TypeError, ValidationError):
        return False


def reviewed_event_proof(
    session: Session, *, anchor: RawItem, anchor_source: Source, target: RawItem,
    target_source: Source | None = None, run: EditorialRun | None = None,
    post: Post | None = None, now: datetime | None = None,
) -> ReviewedEventProof | None:
    """Перепроверить источник подавления; ID, заголовок и пустой skipped недостаточны.

    Квитанция привязана к сохранённому событию, а не к изменяемому текущему raw.
    При неизвестной отправке возможное совпадение требует сверки и не открывает retry.
    """
    if target.id == anchor.id:
        return None
    target_source = target_source or session.get(Source, target.source_id)
    run = run or session.get(EditorialRun, target.id)
    if (target_source is None or target_source.city != anchor_source.city or run is None
            or run.raw_item_id != target.id):
        return None
    post = post or (session.get(Post, run.post_id) if run.post_id else None)
    anchor_document = _document(anchor)
    confirmed_send = bool(run.delivery_status == "sent" and post is not None
                          and type(post.telegram_message_id) is int
                          and post.telegram_message_id > 0)
    transport = bool(run.delivery_status in {"sent", "sending", "uncertain"}
        or run.staging_message_id is not None or post and (
            post.telegram_message_id is not None or post.published_at is not None))
    if not transport:
        created, current = _timestamp(run.created_at), _timestamp(now or datetime.now(UTC))
        if (run.delivery_status != "pending" or post is None or not target_source.enabled
                or run.outcome.get("status") not in {"ready", "human_review"}
                or created is None or current is None
                or not timedelta() <= current-created <= timedelta(hours=6)):
            return None
        from relocant_media.services.material_lifecycle import is_terminal_blocked

        if is_terminal_blocked(session, target.id, now=now or datetime.now(UTC)):
            return None
    try:
        documents = _stored_duplicate_documents(run)
    except (ValueError, TypeError, ValidationError):
        documents = ()
    saved_match = (run.evidence.get("city") == anchor_source.city
                   and any(_same_proven_event(anchor_document, doc) for _, doc, _ in documents))
    possible_saved_match = (run.evidence.get("city") == anchor_source.city and any(same_event(
        _document_candidate(anchor_document, anchor.id), _document_candidate(doc, raw_id),
    ) for raw_id, doc, _ in documents))
    kind: Literal["reviewed", "published", "delivery_reconciliation"]
    if transport:
        if confirmed_send and saved_match:
            kind = "published"
        elif (possible_saved_match or _possible_saved_package_match(
                  run, anchor_document, anchor_source.city)
              or canonicalize_url(anchor.url) == canonicalize_url(target.url)
              or same_event(_document_candidate(anchor_document, anchor.id),
                            _document_candidate(_document(target), target.id))):
            kind = "delivery_reconciliation"
        else:
            return None
    else:
        if (not saved_match or run.delivery_status != "pending" or post is None
                or not target_source.enabled or run.evidence.get("city") != anchor_source.city):
            return None
        try:
            outcome = EditorialOutcome.model_validate(run.outcome)
            created = _timestamp(run.created_at)
            current = _timestamp(now or datetime.now(UTC))
            if (outcome.status not in {PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW}
                    or not outcome.ranking.should_process or outcome.claims is None
                    or outcome.verification is None or outcome.draft is None
                    or (post.title, post.body) != (outcome.draft.title, outcome.draft.body)
                    or post.status != outcome.status.value or created is None or current is None
                    or not timedelta() <= current-created <= timedelta(hours=6)):
                return None
            verdicts = {claim.claim_id: claim.supported for claim in outcome.verification.claims}
            if (len(verdicts) != len(outcome.verification.claims)
                    or set(verdicts) != {claim.claim_id for claim in outcome.claims.claims}
                    or len({claim.claim_id for claim in outcome.claims.claims})
                    != len(outcome.claims.claims)
                    or any(claim.material and not verdicts.get(claim.claim_id)
                           for claim in outcome.claims.claims)
                    or not set(outcome.draft.claim_ids) <= {
                        key for key, supported in verdicts.items() if supported}):
                return None
            for raw_id, document, snapshot in documents:
                item = session.get(RawItem, raw_id)
                source = session.get(Source, item.source_id) if item else None
                if (item is None or source is None or not source.enabled
                        or source.city != anchor_source.city or _document(item) != document
                        or any(snapshot.get(key) != getattr(source, key)
                               for key in ("ownership", "high_risk", "auto_publish"))):
                    return None
            for claim in outcome.claims.claims:
                if any(not any(citation.source_id == doc.source_id and citation.url == doc.url
                               and citation.quote in doc.body for _, doc, _ in documents)
                       for citation in claim.citations):
                    return None
        except (ValueError, TypeError, ValidationError):
            return None
        kind = "reviewed"
    reviewed_at = _timestamp(run.created_at)
    context = {"anchor_raw_id": anchor.id, "anchor": anchor_document.model_dump(mode="json"),
        "target_raw_id": target.id, "post_id": post.id if post else None,
        "outcome_sha256": hashlib.sha256(json.dumps(
            run.outcome, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "kind": kind, "documents": [snapshot for _, _, snapshot in documents],
        "delivery_status": run.delivery_status, "staging_message_id": run.staging_message_id,
        "telegram_message_id": post.telegram_message_id if post else None,
        "reviewed_at": reviewed_at.isoformat() if reviewed_at else None}
    digest = hashlib.sha256(json.dumps(
        context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return ReviewedEventProof(target.id, kind, digest)


def _reviewed_event_candidates(
    session: Session, city: str,
) -> list[tuple[RawItem, Source, EditorialRun, Post | None]]:
    """Один bulk read только потенциального proof; архивные отказы не читаются."""
    transport_scope = or_(EditorialRun.delivery_status.in_({"sent", "sending", "uncertain"}),
        EditorialRun.staging_message_id.is_not(None), Post.telegram_message_id.is_not(None),
        Post.published_at.is_not(None))
    reviewed_scope = and_(EditorialRun.delivery_status == "pending", Post.id.is_not(None),
        EditorialRun.outcome["status"].as_string().in_({"ready", "human_review"}))
    reviewed = session.execute(
        select(RawItem, Source, EditorialRun, Post)
        .join(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .join(Source, Source.id == RawItem.source_id)
        .outerjoin(Post, Post.id == EditorialRun.post_id)
        .where(Source.city == city, or_(transport_scope, reviewed_scope))
        .order_by(RawItem.published_at, RawItem.id)
        .execution_options(populate_existing=True)
    )
    return [(row[0], row[1], row[2], row[3]) for row in reviewed]


def _reviewed_event_match(
    session: Session, *, anchor: RawItem, anchor_source: Source, now: datetime | None = None,
    candidates: list[tuple[RawItem, Source, EditorialRun, Post | None]] | None = None,
) -> tuple[RawItem, ReviewedEventProof] | None:
    rows = (_reviewed_event_candidates(session, anchor_source.city)
            if candidates is None else candidates)
    for item, source, run, post in rows:
        proof = reviewed_event_proof(session, anchor=anchor, anchor_source=anchor_source,
                                    target=item, target_source=source, run=run, post=post, now=now)
        if proof is not None:
            return item, proof
    return None


def _reviewed_event_item(
    session: Session, *, anchor: RawItem, anchor_source: Source,
) -> RawItem | None:
    match = _reviewed_event_match(session, anchor=anchor, anchor_source=anchor_source)
    return match[0] if match is not None else None


def _event_items(
    session: Session,
    *,
    anchor: RawItem,
    anchor_source: Source,
) -> list[tuple[RawItem, Source]]:
    """Return one evidence item per source for the same bounded event."""
    members: list[tuple[RawItem, Source]] = [(anchor, anchor_source)]
    replaces_unavailable = session.scalar(select(EditorialRun.raw_item_id).where(
        EditorialRun.outcome["reason"].as_string() == SOURCE_REPLACED_REASON,
        EditorialRun.outcome["replacement_raw_item_id"].as_integer() == anchor.id,
    ).limit(1))
    if anchor.published_at is None or replaces_unavailable is not None:
        return members

    lower = anchor.published_at - DEFAULT_EVENT_WINDOW
    upper = anchor.published_at + DEFAULT_EVENT_WINDOW
    date_scope = and_(RawItem.published_at >= lower, RawItem.published_at <= upper)
    rows = session.execute(
        select(RawItem, Source)
        .join(Source, Source.id == RawItem.source_id)
        .where(
            RawItem.id != anchor.id,
            Source.city == anchor_source.city,
            Source.enabled.is_(True),
            date_scope,
        )
        .order_by(RawItem.published_at, RawItem.id)
        .execution_options(populate_existing=True)
    ).all()

    anchor_candidate = _candidate(anchor)
    seen_sources = {anchor.source_id}
    for item, source in rows:
        if item.source_id in seen_sources:
            continue
        if not same_event(anchor_candidate, _candidate(item)):
            continue
        members.append((item, source))
        seen_sources.add(item.source_id)
    return members


def _auto_publish_policy_reason(
    members: list[tuple[RawItem, Source]],
    *,
    anchor_source: Source,
    category: str | None = None,
) -> str | None:
    if not anchor_source.auto_publish:
        return "source_auto_publish_disabled"
    if any(source.high_risk for _, source in members):
        return "source_policy_requires_review"
    if anchor_source.source_type == "official_venue_events" and category != "events":
        return "venue_events_only"
    # Official evidence always counts; a verified media outlet only outside high-risk topics.
    if primary_evidence_kind([source.ownership for _, source in members], category or "") is None:
        return "official_source_required"
    return None


def _snapshot_member(item: RawItem, source: Source) -> dict[str, object]:
    document = _document(item).model_dump(mode="json")
    return {
        "raw_item_id": item.id,
        **document,
        "ownership": source.ownership,
        "high_risk": source.high_risk,
        "auto_publish": source.auto_publish,
    }


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    text: str
    message_id: int | None = None


class EditorialService:
    """Use a dedicated session: methods commit their own durable boundaries."""

    def __init__(self, session: Session, pipeline: EditorialPipeline) -> None:
        self.session = session
        self.pipeline = pipeline

    def prepare(self, raw_item_id: int) -> EditorialRun:
        try:
            city = self.session.scalar(select(Source.city).join(
                RawItem, RawItem.source_id == Source.id,
            ).where(RawItem.id == raw_item_id))
            if city is None:
                raise ValueError("raw item/source does not exist")
            _lock_editorial_city(self.session, city)
            item = self.session.scalar(
                select(RawItem).where(RawItem.id == raw_item_id).with_for_update()
                .execution_options(populate_existing=True)
            )
            if item is None:
                raise ValueError("raw item does not exist")
            existing = self.session.get(EditorialRun, raw_item_id)
            if existing is not None:
                self.session.commit()
                return existing
            source = self.session.get(Source, item.source_id, populate_existing=True)
            if source is None:
                raise ValueError("raw item source does not exist")
            from relocant_media.services.material_lifecycle import ensure_not_terminal

            ensure_not_terminal(self.session, raw_item_id)
            if not source.enabled:
                raise ValueError("raw item source is disabled")
            reviewed_event = _reviewed_event_item(
                self.session,
                anchor=item,
                anchor_source=source,
            )
            if reviewed_event is not None:
                raise ValueError(
                    f"event already reviewed via raw item {reviewed_event.id}"
                )

            members = _event_items(self.session, anchor=item, anchor_source=source)
            documents = tuple(_document(member) for member, _member_source in members)
            outcome = self.pipeline.run(city=source.city, documents=documents)
            policy_reason = _auto_publish_policy_reason(
                members, anchor_source=source, category=outcome.ranking.category,
            )
            if outcome.status == PublicationStatus.READY and policy_reason is not None:
                outcome = outcome.model_copy(update={
                    "status": PublicationStatus.HUMAN_REVIEW,
                    "reason": policy_reason,
                })

            post = None
            if outcome.draft is not None and outcome.status in {
                PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW,
            }:
                post = Post(
                    city=source.city,
                    category=outcome.ranking.category,
                    status=outcome.status.value,
                    title=outcome.draft.title,
                    body=outcome.draft.body,
                    confidence=Decimal(str(outcome.confidence)),
                    editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
                )
                self.session.add(post)
                self.session.flush()

            anchor_document = documents[0]
            run = EditorialRun(
                raw_item_id=item.id,
                outcome=outcome.model_dump(mode="json"),
                evidence={
                    **anchor_document.model_dump(mode="json"),
                    "city": source.city,
                    "event_key": event_key(_candidate(item)),
                    "documents": [
                        _snapshot_member(member, member_source)
                        for member, member_source in members
                    ],
                },
                post_id=post.id if post else None,
                editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
                delivery_status="pending",
            )
            self.session.add(run)
            self.session.commit()
            return run
        except BaseException:
            self.session.rollback()
            raise

    def _validate_evidence_bundle(
        self,
        run: EditorialRun,
        *,
        anchor_item: RawItem,
        anchor_source: Source,
    ) -> list[tuple[RawItem, Source]]:
        snapshots = run.evidence.get("documents")
        if not isinstance(snapshots, list):
            return [(anchor_item, anchor_source)]

        current: list[tuple[RawItem, Source]] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("raw_item_id"), int):
                raise DeliveryBlockedError("invalid stored evidence bundle")
            member = self.session.get(RawItem, snapshot["raw_item_id"], populate_existing=True)
            if member is None:
                raise DeliveryBlockedError("event evidence disappeared after verification")
            source = self.session.get(Source, member.source_id, populate_existing=True)
            if source is None or not source.enabled:
                raise DeliveryBlockedError("event evidence source is missing or disabled")
            expected = {
                "source_id": member.source_id,
                "url": member.url,
                "title": member.title,
                "body": member.body,
            }
            if any(snapshot.get(key) != value for key, value in expected.items()):
                raise DeliveryBlockedError("event evidence changed after verification")
            saved = EvidenceDocument.model_validate(snapshot)
            if (saved.published_at_precision != member.published_at_precision
                    or _timestamp(saved.published_at) != _timestamp(member.published_at)):
                raise DeliveryBlockedError(
                    "event evidence publication time changed after verification"
                )
            current.append((member, source))
        return current

    async def deliver(
        self, raw_item_id: int, publisher: Publisher | None = None, *, dry_run: bool = True,
    ) -> DeliveryResult:
        city = self.session.scalar(select(Source.city).join(
            RawItem, RawItem.source_id == Source.id,
        ).where(RawItem.id == raw_item_id))
        if city is None:
            raise DeliveryBlockedError("raw item/source does not exist")
        _lock_editorial_city(self.session, city)
        run = self.session.get(EditorialRun, raw_item_id, populate_existing=True)
        if run is None:
            raise ValueError("prepare the raw item before delivery")
        outcome = EditorialOutcome.model_validate(run.outcome)
        if "_package" in run.outcome:
            from relocant_media.ai.package import EditorialPackage, evaluate_package

            package = EditorialPackage.model_validate_json(
                json.dumps(run.outcome["_package"])
            )
            outcome = evaluate_package(package)
        if outcome.status != PublicationStatus.READY or outcome.draft is None:
            raise DeliveryBlockedError(f"editorial policy blocks delivery: {outcome.status}")
        item = self.session.get(RawItem, raw_item_id, populate_existing=True)
        source = self.session.get(Source, item.source_id, populate_existing=True) if item else None
        post = self.session.get(Post, run.post_id, populate_existing=True) if run.post_id else None
        if item is None or source is None or not source.enabled:
            raise DeliveryBlockedError("source is missing or disabled")
        if source.city != run.evidence.get("city") or post is None or post.city != source.city:
            raise DeliveryBlockedError("editorial city does not match the source")
        if post.status != PublicationStatus.READY:
            raise DeliveryBlockedError("post is no longer ready for delivery")
        if (post.title, post.body) != (outcome.draft.title, outcome.draft.body):
            raise DeliveryBlockedError("post draft changed after verification")
        from relocant_media.domain.draft_language import draft_language_reason
        from relocant_media.domain.freshness import draft_date_reason
        from relocant_media.domain.water_bulletin import draft_water_tanker_reason

        if draft_language_reason(outcome.draft.title, outcome.draft.body) is not None:
            raise DeliveryBlockedError("draft requires Russian translation")
        if draft_date_reason(outcome.draft.title, outcome.draft.body) is not None:
            raise DeliveryBlockedError("draft requires an absolute event date")
        if draft_water_tanker_reason(
            item.url, item.title, outcome.draft.title, outcome.draft.body,
        ) is not None:
            raise DeliveryBlockedError("draft tanker location is not tied to the outage")
        if any(run.evidence.get(key) != value for key, value in {
            "source_id": item.source_id, "url": item.url,
            "title": item.title, "body": item.body,
        }.items()):
            raise DeliveryBlockedError("source evidence changed after verification")
        saved_document = EvidenceDocument.model_validate(run.evidence)
        if (saved_document.published_at_precision != item.published_at_precision
                or _timestamp(saved_document.published_at) != _timestamp(item.published_at)):
            raise DeliveryBlockedError("source publication time changed after verification")

        members = self._validate_evidence_bundle(run, anchor_item=item, anchor_source=source)
        if "_package" in run.outcome and package.presentation is not None:
            from relocant_media.domain.dedup import find_repeated_publication
            from relocant_media.services.editorial_products import (
                published_transport_duplicate,
                recent_publications,
                verify_stored_presentation,
            )

            assert package.event is not None
            try:
                verify_stored_presentation(
                    self.session, item.id, tuple(package.event.documents),
                    document_ids=[member.id for member, _ in members],
                    presentation=package.presentation)
            except ValueError as error:
                raise DeliveryBlockedError(str(error)) from error
            now = datetime.now(UTC)
            recent = recent_publications(self.session, package.city, now=now)
            if outcome.ranking.category == "transport" and package.linked_update is None:
                from relocant_media.services.event_lifecycle import (
                    source_revision_lifecycle_context,
                )

                if source_revision_lifecycle_context(self.session, raw_item_id) is None:
                    duplicate = published_transport_duplicate(recent, outcome.draft, now=now)
                    if duplicate is not None:
                        raise PublishedTransportDuplicate(duplicate)
            repeated = find_repeated_publication(
                outcome.draft.title, outcome.draft.body,
                [(row["id"], row["title"], row["body"]) for row in recent
                 if row["id"] != post.id and (row["status"] == "published" or row["id"] < post.id)])
            if repeated is not None:
                raise DeliveryBlockedError(f"draft repeats recent post {repeated}")
        from relocant_media.domain.audience import audience_relevance_reason

        if source.city == "belgrade":
            for member, _ in members:
                audience_reason = audience_relevance_reason(member.title, member.body)
                if audience_reason:
                    raise DeliveryBlockedError(
                        f"audience policy blocks delivery: {audience_reason}")
        if "_package" in run.outcome and package.version == 2:
            assert package.event is not None
            # A new source, changed membership or policy requires fresh bundle reviews.
            members = _event_items(self.session, anchor=item, anchor_source=source)
            expected = {d.raw_item_id: d.model_dump(mode="json")
                        for d in package.event.documents}
            actual = {member.id: _snapshot_member(member, policy)
                      for member, policy in members}
            if (package.event.event_key != event_key(_candidate(item))
                    or expected != actual):
                raise DeliveryBlockedError("event evidence or source policy changed")
        policy_reason = _auto_publish_policy_reason(
            members, anchor_source=source, category=outcome.ranking.category,
        )
        if policy_reason is not None:
            raise DeliveryBlockedError(f"source policy blocks delivery: {policy_reason}")
        if primary_evidence_kind(
            [member_source.ownership for _, member_source in members],
            outcome.ranking.category,
        ) == "media" and copies_source_text(
            f"{outcome.draft.title}\n{outcome.draft.body}",
            [f"{member.title}\n{member.body}" for member, _ in members],
        ):
            raise DeliveryBlockedError("draft copies the media source text")

        primary = next(
            (member for member, member_source in members
             if member_source.ownership.strip().casefold() == "official"),
            item,
        )
        text = f"{outcome.draft.title}\n\n{outcome.draft.body}\n\nИсточник: {primary.url}"
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise DeliveryBlockedError("draft exceeds Telegram message length")
        if dry_run:
            return DeliveryResult("dry_run", text)
        if publisher is None:
            raise ValueError("a staging publisher is required")
        # Commit BEFORE the external request. A crash or timeout must never trigger an
        # automatic retry: Telegram sendMessage has no idempotency key.
        claimed = cast(CursorResult[object], self.session.execute(
            update(EditorialRun).where(
                EditorialRun.raw_item_id == raw_item_id,
                EditorialRun.delivery_status == "pending",
            ).values(delivery_status="sending")
        ))
        self.session.commit()
        if claimed.rowcount != 1:
            self.session.refresh(run)
            if run.delivery_status == "sent":
                return DeliveryResult("sent", text, run.staging_message_id)
            raise DeliveryBlockedError("delivery outcome requires manual reconciliation")
        try:
            message_id = await publisher.publish(text)
        except BaseException:
            self.session.execute(update(EditorialRun).where(
                EditorialRun.raw_item_id == raw_item_id,
            ).values(delivery_status="uncertain"))
            self.session.commit()
            raise
        self.session.execute(update(EditorialRun).where(
            EditorialRun.raw_item_id == raw_item_id,
        ).values(delivery_status="sent", staging_message_id=message_id))
        self.session.commit()
        return DeliveryResult("sent", text, message_id)
