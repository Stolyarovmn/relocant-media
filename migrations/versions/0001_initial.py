"""Initial operational schema.

Revision ID: 0001
Revises:
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("trust", sa.Numeric(4, 3), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "raw_item",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(), sa.ForeignKey("source.id"), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("source_id", "external_id", name="uq_raw_item_source_external"),
    )
    op.create_table(
        "post",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "post_snapshot",
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("post.id"), primary_key=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("views", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reactions", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("comments", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("forwards", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("subscriber_delta", sa.BigInteger()),
    )
    op.create_table(
        "submission",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "moderation_action",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("model", sa.String()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "ad_request",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger()),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("quoted_price", sa.Numeric(12, 2)),
        sa.Column("currency", sa.String()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "daily_report",
        sa.Column("report_date", sa.Date(), primary_key=True),
        sa.Column("city", sa.String(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("daily_report")
    op.drop_table("ad_request")
    op.drop_table("moderation_action")
    op.drop_table("submission")
    op.drop_table("post_snapshot")
    op.drop_table("post")
    op.drop_table("raw_item")
    op.drop_table("source")
