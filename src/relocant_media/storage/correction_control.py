"""Append-only editorial decisions for private reader corrections."""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from relocant_media.storage.models import Base
from relocant_media.storage.types import AUTOINCREMENT_ID, UTC_DATE


class ReaderCorrectionDecision(Base):
    __tablename__ = "reader_correction_decision"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_reader_correction_decision_idempotency"),
        Index(
            "ix_reader_correction_decision_submission_decided",
            "submission_id",
            "decided_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submission.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        UTC_DATE,
        server_default=func.now(),
        nullable=False,
    )
