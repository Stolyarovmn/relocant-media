from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer


class EvidenceDocument(BaseModel):
    source_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    title: str = Field(min_length=1)
    body: str = Field(min_length=1)
    published_at: datetime | None = None
    published_at_precision: str = Field(default="unknown", pattern="^(unknown|day|instant)$")


class EvidenceCitation(BaseModel):
    source_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class SelectionCitation(EvidenceCitation):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    quote: str = Field(min_length=8, max_length=800)


class SelectionBrief(BaseModel):
    """Основание отбора, привязанное к точной конфигурации и исходным цитатам."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    policy_version: str = Field(min_length=1, max_length=100)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    audience: str = Field(min_length=8, max_length=400)
    benefit: str = Field(min_length=12, max_length=600)
    novelty: str = Field(min_length=12, max_length=600)
    why_now: str = Field(min_length=12, max_length=600)
    rubric_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    locality: Literal["city", "country"]
    value_kind: Literal["action", "understanding", "experience", "connection"]
    evidence_refs: tuple[SelectionCitation, ...] = Field(min_length=1, max_length=5)


class RankingResult(BaseModel):
    should_process: bool
    relevance: float = Field(ge=0.0, le=1.0)
    category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    selection: SelectionBrief | None = None

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        # Старые пакеты и аттестации сохраняют прежний байтовый JSON-контракт.
        if self.selection is None:
            payload.pop("selection", None)
        return payload


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
