"""Append-only review records for advertiser suggested-post candidates."""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from relocant_media.storage.models import Base
from relocant_media.storage.types import AUTOINCREMENT_ID, JSON_VALUE, UTC_DATE


class SuggestedPostReview(Base):
    __tablename__ = "suggested_post_review"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_suggested_post_review_idempotency"),
        Index(
            "ix_suggested_post_review_request_reviewed",
            "ad_request_id",
            "reviewed_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    ad_request_id: Mapped[int] = mapped_column(ForeignKey("ad_request.id"), nullable=False)
    reviewed_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_outcome: Mapped[str] = mapped_column(String, nullable=False)
    effective_outcome: Mapped[str] = mapped_column(String, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    risk_flags: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    audience_fit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    claims_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requires_owner_approval: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    reviewer: Mapped[str] = mapped_column(String, nullable=False)
    reviewer_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(
        UTC_DATE,
        server_default=func.now(),
        nullable=False,
    )
