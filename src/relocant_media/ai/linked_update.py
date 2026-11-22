"""Проверяемая ссылка на собственную опубликованную новость; сама не даёт допуска."""
from __future__ import annotations

from typing import TYPE_CHECKING, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from relocant_media.domain.source_locations import is_public_https_url

if TYPE_CHECKING:
    from relocant_media.ai.package import EditorialPackage


class PublishedPostReference(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    raw_item_id: int = Field(gt=0)
    message_id: int = Field(gt=0)
    channel_id: int = Field(lt=0)
    source_id: str = Field(min_length=1, max_length=200)
    source_url: str = Field(max_length=2048)
    public_url: str = Field(max_length=2048)
    source_type: str = Field(min_length=1, max_length=100)
    ownership: str = Field(pattern="^(official|media)$")
    high_risk: bool
    auto_publish: bool
    title: str = Field(min_length=1, max_length=1000)
    event_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    post_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: AwareDatetime

    @field_validator("source_url", "public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        if not is_public_https_url(value):
            raise ValueError("Нужен публичный HTTPS-адрес")
        return value

    @model_validator(mode="after")
    def low_risk(self) -> Self:
        if self.high_risk:
            raise ValueError("Чувствительная публикация не входит в автоматический контекст")
        return self


class LinkedUpdate(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    parent: PublishedPostReference
    change_claim_ids: tuple[str, ...] = Field(min_length=1, max_length=30)


class LinkedUpdateFactsCheck(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    parent_message_id: int = Field(gt=0)
    parent_publication_checked: bool
    same_event: bool
    substantive_new_facts: bool
    change_claim_ids: tuple[str, ...] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=1)


def linked_update_review_reason(
    package: EditorialPackage, check: LinkedUpdateFactsCheck | None,
) -> str | None:
    link = package.linked_update
    if link is None:
        return "linked_update_context_missing" if check is not None else None
    ids = link.change_claim_ids
    claims = package.outcome.claims
    verification = package.outcome.verification
    draft = package.outcome.draft
    assert claims is not None and verification is not None and draft is not None
    supported = {claim.claim_id for claim in verification.claims if claim.supported}
    material = {claim.claim_id for claim in claims.claims if claim.material}
    allowed = set(draft.claim_ids) & supported & material
    if len(set(ids)) != len(ids) or not set(ids).issubset(allowed):
        return "linked_update_invalid_claims"
    if (check is None or not check.parent_publication_checked or not check.same_event
            or not check.substantive_new_facts
            or check.parent_message_id != link.parent.message_id
            or check.change_claim_ids != ids):
        return "linked_update_review_required"
    return None
