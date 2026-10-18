from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class EvidenceDocument(BaseModel):
    source_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    published_at: datetime | None = None


class RankingResult(BaseModel):
    should_process: bool
    relevance: float = Field(ge=0.0, le=1.0)
    category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)


class EvidenceCitation(BaseModel):
    source_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class ExtractedClaim(BaseModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    material: bool = True
    citations: tuple[EvidenceCitation, ...] = Field(min_length=1)


class ClaimSet(BaseModel):
    claims: tuple[ExtractedClaim, ...] = Field(min_length=1)


class ClaimVerification(BaseModel):
    claim_id: str = Field(min_length=1)
    supported: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)


class VerificationResult(BaseModel):
    claims: tuple[ClaimVerification, ...] = Field(min_length=1)


class RussianDraft(BaseModel):
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    claim_ids: tuple[str, ...] = Field(min_length=1)


class PublicationStatus(StrEnum):
    READY = "ready"
    HUMAN_REVIEW = "human_review"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class EditorialOutcome(BaseModel):
    status: PublicationStatus
    reason: str
    ranking: RankingResult
    claims: ClaimSet | None = None
    verification: VerificationResult | None = None
    draft: RussianDraft | None = None
    confidence: float = Field(ge=0.0, le=1.0)
