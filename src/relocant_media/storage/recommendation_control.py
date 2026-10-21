"""Auditable recommendation proposals and external GitHub target links."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from relocant_media.storage.models import Base
from relocant_media.storage.types import AUTOINCREMENT_ID, JSON_VALUE, UTC_DATE


class RecommendationProposal(Base):
    __tablename__ = "recommendation_proposal"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_recommendation_proposal_fingerprint"),
        Index(
            "ix_recommendation_proposal_city_date",
            "city",
            "report_date",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String, nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    recommendation_code: Mapped[str] = mapped_column(String, nullable=False)
    action_class: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    github_issue_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE,
        server_default=func.now(),
        nullable=False,
    )


class RecommendationTargetLink(Base):
    __tablename__ = "recommendation_target_link"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_recommendation_target_link_idempotency"),
        UniqueConstraint(
            "proposal_id",
            "target_kind",
            "target_repo",
            "target_number",
            name="uq_recommendation_target_link_target",
        ),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("recommendation_proposal.id"),
        nullable=False,
    )
    target_kind: Mapped[str] = mapped_column(String, nullable=False)
    target_repo: Mapped[str] = mapped_column(String, nullable=False)
    target_number: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        UTC_DATE,
        server_default=func.now(),
        nullable=False,
    )
