"""Versioned external editorial packages. Review identities are local attestations."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from relocant_media.ai.draft_coverage import DraftFactsCheck, draft_coverage_reason
from relocant_media.ai.linked_update import LinkedUpdate, linked_update_review_reason
from relocant_media.ai.models import (
    ClaimSet,
    ClaimVerification,
    EditorialOutcome,
    EvidenceCitation,
    EvidenceDocument,
    ExtractedClaim,
    RankingResult,
    RussianDraft,
    VerificationResult,
)
from relocant_media.ai.pipeline import _citations_are_grounded, _verification_map
from relocant_media.domain.audience import official_water_notice
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.freshness import has_draft_calendar_day
from relocant_media.domain.policy import AUTO_PUBLISH_CATEGORIES, requires_human_review
from relocant_media.domain.sensitivity import personal_detail_reason, sensitive_topic_reason


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class PackageEvidence(EvidenceDocument, StrictModel):
    pass


class PackageCitation(EvidenceCitation, StrictModel):
    pass


class PackageClaim(ExtractedClaim, StrictModel):
    citations: tuple[PackageCitation, ...] = Field(min_length=1)


class PackageClaims(ClaimSet, StrictModel):
    claims: tuple[PackageClaim, ...] = Field(min_length=1)


class PackageRanking(RankingResult, StrictModel):
    pass


class PackageVerdict(ClaimVerification, StrictModel):
    pass


class PackageVerification(VerificationResult, StrictModel):
    claims: tuple[PackageVerdict, ...] = Field(min_length=1)


class PackageDraft(RussianDraft, StrictModel):
    pass


class PackageOutcome(EditorialOutcome, StrictModel):
    ranking: PackageRanking
    claims: PackageClaims
    verification: PackageVerification
    draft: PackageDraft


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def draft_sha256(draft: RussianDraft) -> str:
    """Lowercase SHA256 of UTF-8 canonical JSON {title,body,claim_ids}; no newline."""
    return hashlib.sha256(canonical_json(draft.model_dump(mode="json")).encode()).hexdigest()


class ReviewVerdict(StrictModel):
    reviewer_id: str = Field(min_length=1)
    role: Literal["facts", "utility"]
    source_id: str = Field(min_length=1)
    article_id: str = Field(min_length=1)
    draft_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    verdict: Literal["approve", "hold", "reject"]
    score: int = Field(ge=0, le=100)
    flags: tuple[str, ...]
    live_source_checked: bool = False
    facts_check: DraftFactsCheck | None = None

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        if self.facts_check is None:
            payload.pop("facts_check", None)
        return payload


class PackageEventDocument(PackageEvidence):
    raw_item_id: int = Field(gt=0)
    ownership: str = Field(min_length=1)
    high_risk: bool
    auto_publish: bool


class PackageEvent(StrictModel):
    event_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    editorial_rule_version: Literal["belgrade-editorial-v2"]
    documents: tuple[PackageEventDocument, ...] = Field(min_length=1)


class EditorialPackage(StrictModel):
    version: Literal[1, 2]
    policy_version: Literal["subscription-editorial-v1", "subscription-editorial-v2"]
    city: str = Field(min_length=1)
    raw_item_id: int = Field(gt=0)
    source_id: str = Field(min_length=1)
    article_id: str = Field(min_length=1)
    urgent: bool = False
    evidence: PackageEvidence
    outcome: PackageOutcome
    reviews: tuple[ReviewVerdict, ReviewVerdict]
    event: PackageEvent | None = None
    presentation: EditorialPresentation | None = None
    linked_update: LinkedUpdate | None = None

    @model_validator(mode="after")
    def validate_version_contract(self) -> Self:
        if self.policy_version != f"subscription-editorial-v{self.version}":
            raise ValueError("package policy/version mismatch")
        if self.version == 1:
            if self.linked_update is not None:
                raise ValueError("linked update requires package v2")
            if self.presentation is not None or self.outcome.ranking.selection is not None:
                raise ValueError("selection and presentation require package v2")
            if self.event is not None:
                raise ValueError("package v1 cannot attest event evidence")
        else:
            if self.event is None:
                raise ValueError("package v2 requires event evidence")
            ids = [document.raw_item_id for document in self.event.documents]
            sources = [document.source_id for document in self.event.documents]
            if len(ids) != len(set(ids)) or len(sources) != len(set(sources)):
                raise ValueError("duplicate event document/source")
            primary = next((d for d in self.event.documents
                            if d.raw_item_id == self.raw_item_id), None)
            if primary is None or any(
                getattr(primary, field) != getattr(self.evidence, field)
                for field in type(self.evidence).model_fields
            ):
                raise ValueError("event evidence must include exact primary document")
        return self

    @model_serializer(mode="wrap")
    def serialize_version(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        # Keep historical v1 hashes and persisted receipts byte-for-byte compatible.
        if self.version == 1:
            payload.pop("event", None)
        if self.presentation is None:
            payload.pop("presentation", None)
        if self.linked_update is None:
            payload.pop("linked_update", None)
        return payload

    @field_validator("version", mode="before")
    @classmethod
    def version_is_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value


def context_sha256(package: EditorialPackage) -> str:
    """Bind all package fields except reviews using the same canonical UTF-8 JSON."""
    context = package.model_dump(mode="json", exclude={"reviews"})
    return hashlib.sha256(canonical_json(context).encode()).hexdigest()


def package_coverage_reason(package: EditorialPackage) -> str | None:
    """Общий допуск фактического покрытия для импорта и ожидающей доставки."""
    outcome = package.outcome
    documents = (tuple(package.event.documents) if package.event is not None
                 else (package.evidence,))
    facts = next((review for review in package.reviews if review.role == "facts"), None)
    reason = draft_coverage_reason(
        facts.facts_check if facts else None, outcome.draft, outcome.claims,
        outcome.verification, documents, outcome.ranking.category,
    )
    if reason is not None:
        return reason
    return linked_update_review_reason(
        package, facts.facts_check.linked_update_check if facts and facts.facts_check else None,
    )


def package_facts_scope(package: EditorialPackage) -> str | None:
    facts = next((review for review in package.reviews if review.role == "facts"), None)
    return facts.facts_check.publication_scope if facts and facts.facts_check else None


def package_requires_official_primary(package: EditorialPackage) -> bool:
    """Независимо выявленная чувствительность повышает требования к первоисточнику."""
    return package_facts_scope(package) == "sensitive_substance"


def _package_texts(package: EditorialPackage) -> list[str]:
    documents = (package.event.documents if package.event is not None
                 else (package.evidence,))
    texts = [package.evidence.title, package.evidence.body]
    for document in documents:
        texts.extend((document.title, document.body))
    texts.extend((package.outcome.draft.title, package.outcome.draft.body))
    return texts


def has_official_water_notice(package: EditorialPackage) -> bool:
    """Check the narrow source and date rule for automatic water announcements."""
    draft = package.outcome.draft
    return (
        package.outcome.ranking.category == "utilities" and package.event is not None
        and has_draft_calendar_day(draft.title, draft.body)
        and any(
            document.raw_item_id == package.raw_item_id and document.ownership == "official"
            and document.auto_publish and not document.high_risk
            and official_water_notice(document.source_id, document.url, document.title,
                                      document.body)
            for document in package.event.documents
        )
    )


def evaluate_package(package: EditorialPackage, *, now: datetime | None = None) -> EditorialOutcome:
    """Recompute eligibility; an asserted ready/should_process is never sufficient."""
    from relocant_media.ai.models import PublicationStatus
    from relocant_media.domain.draft_language import draft_language_reason
    from relocant_media.domain.freshness import draft_date_reason
    from relocant_media.domain.water_bulletin import draft_water_tanker_reason

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("evaluation time must be timezone-aware")
    outcome = EditorialOutcome.model_validate(package.outcome.model_dump())
    claims, verification, draft = outcome.claims, outcome.verification, outcome.draft
    assert claims is not None and verification is not None and draft is not None
    if package.source_id != package.evidence.source_id:
        raise ValueError("package source does not match evidence")
    documents = (tuple(package.event.documents) if package.event is not None
                 else (package.evidence,))
    if outcome.ranking.selection is not None:
        from relocant_media.ai.selection import selection_reason
        from relocant_media.domain.city_profile import load_city_profile
        from relocant_media.services.editorial_products import validate_presentation

        profile = load_city_profile(package.city)
        reason = selection_reason(outcome.ranking, profile, documents)
        if reason:
            raise ValueError(reason)
        if package.presentation is None:
            raise ValueError("selected package requires reviewed presentation")
        validate_presentation(package.presentation, profile, documents, draft,
                              rubric_id=outcome.ranking.selection.rubric_id,
                              category=outcome.ranking.category)
    elif package.presentation is not None:
        raise ValueError("presentation requires selection brief")
    if not _citations_are_grounded(claims, documents):
        raise ValueError("package contains ungrounded citations")
    verdicts = _verification_map(verification, claims)
    if verdicts is None:
        raise ValueError("verification claim ids do not match")
    if any(claim.material and not verdicts[claim.claim_id][0] for claim in claims.claims):
        raise ValueError("unsupported material claim")
    supported = {claim_id for claim_id, (ok, _) in verdicts.items() if ok}
    if not set(draft.claim_ids).issubset(supported):
        raise ValueError("draft references unverified claims")
    if len(set(draft.claim_ids)) != len(draft.claim_ids):
        raise ValueError("duplicate draft claim ids")
    if {review.role for review in package.reviews} != {"facts", "utility"}:
        raise ValueError("both facts and utility review roles are required")
    identities = {review.reviewer_id.strip().casefold() for review in package.reviews}
    if "" in identities or len(identities) != 2:
        raise ValueError("two distinct reviewer identities are required")
    for review in package.reviews:
        if review.context_sha256 != context_sha256(package):
            raise ValueError("review context does not match the editorial package")
        if (review.source_id, review.article_id, review.draft_sha256) != (
            package.source_id, package.article_id, draft_sha256(draft),
        ):
            raise ValueError("review does not match source/article/exact draft")
    confidence = min(outcome.confidence, outcome.ranking.confidence,
                     *(confidence for _, confidence in verdicts.values()))
    official_water = has_official_water_notice(package)
    status, reason = outcome.status, outcome.reason
    if status == PublicationStatus.READY or (
        official_water and status == PublicationStatus.HUMAN_REVIEW
        and reason == "package_requires_review"
    ):
        status, reason = PublicationStatus.HUMAN_REVIEW, "package_requires_review"
        eligible = (
            (outcome.ranking.category.strip().lower() in AUTO_PUBLISH_CATEGORIES or official_water)
            and package.outcome.ranking.should_process
            and not requires_human_review(outcome.ranking.category, confidence)
            and all(
                review.verdict == "approve" and review.score >= 75
                and not review.flags and review.live_source_checked
                and timedelta(0) <= now - review.reviewed_at
                <= timedelta(hours=2 if package.urgent else 24)
                for review in package.reviews
            )
        )
        if eligible:
            status, reason = PublicationStatus.READY, "package_independently_reviewed"
    coverage_reason = package_coverage_reason(package)
    if coverage_reason is not None and status in {
            PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW}:
        if coverage_reason != "independent_sensitivity_review_required":
            status, reason = PublicationStatus.HUMAN_REVIEW, coverage_reason
        elif status == PublicationStatus.READY:
            status, reason = PublicationStatus.HUMAN_REVIEW, "package_requires_review"
    language_reason = draft_language_reason(draft.title, draft.body)
    if language_reason is not None:
        status, reason = PublicationStatus.HUMAN_REVIEW, language_reason
    sensitive_reason = sensitive_topic_reason(_package_texts(package))
    if sensitive_reason is not None and status == PublicationStatus.READY:
        status, reason = PublicationStatus.HUMAN_REVIEW, sensitive_reason
    detail_reason = personal_detail_reason(_package_texts(package), draft.title, draft.body)
    if detail_reason is not None and status in {
            PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW}:
        status, reason = PublicationStatus.HUMAN_REVIEW, detail_reason
    tanker_reason = draft_water_tanker_reason(
        package.evidence.url, package.evidence.title, draft.title, draft.body,
    )
    if tanker_reason is not None and status in {
        PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW,
    }:
        status, reason = PublicationStatus.HUMAN_REVIEW, tanker_reason
    date_reason = draft_date_reason(draft.title, draft.body)
    if date_reason is not None:
        status, reason = PublicationStatus.HUMAN_REVIEW, date_reason
    from relocant_media.domain.event_map_policy import event_map_review_reason

    map_reason = event_map_review_reason(
        package.presentation, outcome.ranking.category, draft.title, draft.body, city=package.city,
    )
    if map_reason is not None and status == PublicationStatus.READY:
        status, reason = PublicationStatus.HUMAN_REVIEW, map_reason
    return outcome.model_copy(update={"status": status, "reason": reason, "confidence": confidence})
