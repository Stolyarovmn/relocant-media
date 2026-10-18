from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from relocant_media.storage.models import Base


class SchedulerLease(Base):
    __tablename__ = "scheduler_lease"

    task_key: Mapped[str] = mapped_column(String, primary_key=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SchedulerRun(Base):
    __tablename__ = "scheduler_run"
    __table_args__ = (
        UniqueConstraint(
            "task_key",
            "scheduled_for",
            "attempt",
            name="uq_scheduler_run_task_schedule_attempt",
        ),
        Index("ix_scheduler_run_task_started", "task_key", "started_at"),
        Index("ix_scheduler_run_city_started", "city", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String, nullable=False)
    task_type: Mapped[str] = mapped_column(String, nullable=False)
    city: Mapped[str] = mapped_column(String, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
