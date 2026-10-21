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
from relocant_media.domain.policy import requires_human_review


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

    @model_validator(mode="after")
    def validate_version_contract(self) -> Self:
        if self.policy_version != f"subscription-editorial-v{self.version}":
            raise ValueError("package policy/version mismatch")
        if self.version == 1:
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


def evaluate_package(package: EditorialPackage, *, now: datetime | None = None) -> EditorialOutcome:
    """Recompute eligibility; an asserted ready/should_process is never sufficient."""
    from relocant_media.ai.models import PublicationStatus
    from relocant_media.domain.draft_language import draft_language_reason

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
    status, reason = outcome.status, outcome.reason
    if status == PublicationStatus.READY:
        status, reason = PublicationStatus.HUMAN_REVIEW, "package_requires_review"
        eligible = (
            package.outcome.ranking.category.strip().lower() in {"transport", "events"}
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
    language_reason = draft_language_reason(draft.title, draft.body)
    if language_reason is not None:
        status, reason = PublicationStatus.HUMAN_REVIEW, language_reason
    return outcome.model_copy(update={"status": status, "reason": reason, "confidence": confidence})
