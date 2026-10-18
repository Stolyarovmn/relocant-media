"""Add Telegram runtime idempotency and intake state.

Revision ID: 0003
Revises: 0002
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_update_receipt",
        sa.Column("update_id", sa.BigInteger(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="processing"),
        sa.Column("error", sa.Text()),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "telegram_conversation",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True),
        sa.Column("pending_kind", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("telegram_conversation")
    op.drop_table("telegram_update_receipt")
