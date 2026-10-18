"""Add operational scheduler lease and audit tables.

Revision ID: 0005
Revises: 0004
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_lease",
        sa.Column("task_key", sa.String(), primary_key=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "scheduler_run",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_key", sa.String(), nullable=False),
        sa.Column("task_type", sa.String(), nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_type", sa.String()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "task_key",
            "scheduled_for",
            "attempt",
            name="uq_scheduler_run_task_schedule_attempt",
        ),
    )
    op.create_index(
        "ix_scheduler_run_task_started",
        "scheduler_run",
        ["task_key", "started_at"],
    )
    op.create_index(
        "ix_scheduler_run_city_started",
        "scheduler_run",
        ["city", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduler_run_city_started", table_name="scheduler_run")
    op.drop_index("ix_scheduler_run_task_started", table_name="scheduler_run")
    op.drop_table("scheduler_run")
    op.drop_table("scheduler_lease")
