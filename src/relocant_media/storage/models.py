from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from relocant_media.domain.policy import CURRENT_EDITORIAL_RULE_VERSION
from relocant_media.storage.types import AUTOINCREMENT_ID, JSON_VALUE, UTC_DATE


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "source"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    city: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    trust: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    ownership: Mapped[str] = mapped_column(
        String, nullable=False, default="unknown", server_default="unknown"
    )
    high_risk: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    auto_publish: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )


class RawItem(Base):
    __tablename__ = "raw_item"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_item_source_external"),
        Index("ix_raw_item_canonical_url", "canonical_url"),
        Index("ix_raw_item_content_sha256", "content_sha256"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTC_DATE)
    ingested_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class SourceRevision(Base):
    __tablename__ = "source_revision"
    __table_args__ = (
        UniqueConstraint("raw_item_id", "snapshot_sha256", name="uq_source_revision_snapshot"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_item.id"), nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False,
    )


class CollectorFailure(Base):
    __tablename__ = "collector_failure"

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("source.id"), nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    error_type: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class Post(Base):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    editorial_rule_version: Mapped[str] = mapped_column(
        String, nullable=False, default=CURRENT_EDITORIAL_RULE_VERSION, server_default="legacy-v1"
    )
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(UTC_DATE)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class EditorialRun(Base):
    __tablename__ = "editorial_run"

    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_item.id"), primary_key=True)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    post_id: Mapped[int | None] = mapped_column(ForeignKey("post.id"), unique=True)
    editorial_rule_version: Mapped[str] = mapped_column(
        String, nullable=False, default=CURRENT_EDITORIAL_RULE_VERSION, server_default="legacy-v1"
    )
    delivery_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    staging_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class PostSnapshot(Base):
    __tablename__ = "post_snapshot"

    post_id: Mapped[int] = mapped_column(ForeignKey("post.id"), primary_key=True)
    captured_at: Mapped[datetime] = mapped_column(UTC_DATE, primary_key=True)
    horizon_hours: Mapped[int | None] = mapped_column(Integer)
    views: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reactions: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    comments: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    forwards: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    subscriber_delta: Mapped[int | None] = mapped_column(BigInteger)


class ChannelSnapshot(Base):
    __tablename__ = "channel_snapshot"
    __table_args__ = (
        CheckConstraint(
            "subscribers >= 0",
            name="ck_channel_snapshot_subscribers_nonnegative",
        ),
        Index("ix_channel_snapshot_city_captured_at", "city", "captured_at"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(UTC_DATE, nullable=False)
    subscribers: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="observed",
        server_default="observed",
    )
    note: Mapped[str | None] = mapped_column(Text)


class AcquisitionCohort(Base):
    __tablename__ = "acquisition_cohort"
    __table_args__ = (
        CheckConstraint("joins >= 0", name="ck_acquisition_joins_nonnegative"),
        CheckConstraint(
            "retained_d1 is null or (retained_d1 >= 0 and retained_d1 <= joins)",
            name="ck_acquisition_retained_d1_range",
        ),
        CheckConstraint(
            "retained_d7 is null or (retained_d7 >= 0 and retained_d7 <= joins)",
            name="ck_acquisition_retained_d7_range",
        ),
        CheckConstraint(
            "retained_d30 is null or (retained_d30 >= 0 and retained_d30 <= joins)",
            name="ck_acquisition_retained_d30_range",
        ),
        UniqueConstraint(
            "city",
            "campaign",
            "cohort_date",
            name="uq_acquisition_city_campaign_date",
        ),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String, nullable=False)
    campaign: Mapped[str] = mapped_column(String, nullable=False)
    cohort_date: Mapped[date] = mapped_column(Date, nullable=False)
    spend: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String)
    joins: Mapped[int] = mapped_column(BigInteger, nullable=False)
    retained_d1: Mapped[int | None] = mapped_column(BigInteger)
    retained_d7: Mapped[int | None] = mapped_column(BigInteger)
    retained_d30: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default="manual",
        server_default="manual",
    )


class AIUsageEvent(Base):
    __tablename__ = "ai_usage_event"
    __table_args__ = (
        CheckConstraint(
            "input_tokens is null or input_tokens >= 0",
            name="ck_ai_input_tokens_nonnegative",
        ),
        CheckConstraint(
            "output_tokens is null or output_tokens >= 0",
            name="ck_ai_output_tokens_nonnegative",
        ),
        CheckConstraint(
            "cost_amount is null or cost_amount >= 0",
            name="ck_ai_cost_nonnegative",
        ),
        Index("ix_ai_usage_event_city_occurred_at", "city", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTC_DATE, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    task: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    currency: Mapped[str | None] = mapped_column(String)
    error_type: Mapped[str | None] = mapped_column(String)


class Submission(Base):
    __tablename__ = "submission"

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="new", server_default="new"
    )
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class ModerationAction(Base):
    __tablename__ = "moderation_action"

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    city: Mapped[str | None] = mapped_column(String)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipt"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="processing", server_default="processing"
    )
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(UTC_DATE)


class TelegramConversation(Base):
    __tablename__ = "telegram_conversation"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pending_kind: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdRequest(Base):
    __tablename__ = "ad_request"

    id: Mapped[int] = mapped_column(AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="new", server_default="new"
    )
    quoted_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )


class DailyReport(Base):
    __tablename__ = "daily_report"

    report_date: Mapped[date] = mapped_column(Date, primary_key=True)
    city: Mapped[str] = mapped_column(String, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )
