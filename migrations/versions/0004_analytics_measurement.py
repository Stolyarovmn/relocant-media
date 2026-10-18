"""Add analytics measurement and acquisition tables.

Revision ID: 0004
Revises: 0003
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("post_snapshot", sa.Column("horizon_hours", sa.Integer()))
    op.add_column("moderation_action", sa.Column("city", sa.String()))

    op.create_table(
        "channel_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subscribers", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="observed"),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("subscribers >= 0", name="ck_channel_snapshot_subscribers_nonnegative"),
    )
    op.create_index(
        "ix_channel_snapshot_city_captured_at",
        "channel_snapshot",
        ["city", "captured_at"],
    )

    op.create_table(
        "acquisition_cohort",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("campaign", sa.String(), nullable=False),
        sa.Column("cohort_date", sa.Date(), nullable=False),
        sa.Column("spend", sa.Numeric(14, 2)),
        sa.Column("currency", sa.String()),
        sa.Column("joins", sa.BigInteger(), nullable=False),
        sa.Column("retained_d1", sa.BigInteger()),
        sa.Column("retained_d7", sa.BigInteger()),
        sa.Column("retained_d30", sa.BigInteger()),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.CheckConstraint("joins >= 0", name="ck_acquisition_joins_nonnegative"),
        sa.CheckConstraint(
            "retained_d1 is null or (retained_d1 >= 0 and retained_d1 <= joins)",
            name="ck_acquisition_retained_d1_range",
        ),
        sa.CheckConstraint(
            "retained_d7 is null or (retained_d7 >= 0 and retained_d7 <= joins)",
            name="ck_acquisition_retained_d7_range",
        ),
        sa.CheckConstraint(
            "retained_d30 is null or (retained_d30 >= 0 and retained_d30 <= joins)",
            name="ck_acquisition_retained_d30_range",
        ),
        sa.UniqueConstraint(
            "city",
            "campaign",
            "cohort_date",
            name="uq_acquisition_city_campaign_date",
        ),
    )

    op.create_table(
        "ai_usage_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("cost_amount", sa.Numeric(14, 6)),
        sa.Column("currency", sa.String()),
        sa.Column("error_type", sa.String()),
        sa.CheckConstraint(
            "input_tokens is null or input_tokens >= 0",
            name="ck_ai_input_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "output_tokens is null or output_tokens >= 0",
            name="ck_ai_output_tokens_nonnegative",
        ),
        sa.CheckConstraint(
            "cost_amount is null or cost_amount >= 0",
            name="ck_ai_cost_nonnegative",
        ),
    )
    op.create_index(
        "ix_ai_usage_event_city_occurred_at",
        "ai_usage_event",
        ["city", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_usage_event_city_occurred_at", table_name="ai_usage_event")
    op.drop_table("ai_usage_event")
    op.drop_table("acquisition_cohort")
    op.drop_index("ix_channel_snapshot_city_captured_at", table_name="channel_snapshot")
    op.drop_table("channel_snapshot")
    op.drop_column("moderation_action", "city")
    op.drop_column("post_snapshot", "horizon_hours")
