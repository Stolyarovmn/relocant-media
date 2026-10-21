"""Append-only source revision control records.

This table stores operator/agent control decisions about a detected source revision.  A decision
never reuses an old editorial approval and never makes a stale EditorialRun publishable; only a
fresh editorial run created after the revision can clear the existing re-verification guard.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from relocant_media.storage.models import Base
from relocant_media.storage.types import AUTOINCREMENT_ID, UTC_DATE


class SourceRevisionDecision(Base):
    __tablename__ = "source_revision_decision"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_source_revision_decision_idempotency"),
        Index("ix_source_revision_decision_revision_decided", "revision_id", "decided_at", "id"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    revision_id: Mapped[int] = mapped_column(ForeignKey("source_revision.id"), nullable=False)
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
