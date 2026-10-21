"""add append-only suggested-post reviews

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suggested_post_review",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ad_request_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("proposed_outcome", sa.String(), nullable=False),
        sa.Column("effective_outcome", sa.String(), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("audience_fit", sa.Boolean(), nullable=False),
        sa.Column("claims_verified", sa.Boolean(), nullable=False),
        sa.Column("requires_owner_approval", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("reviewer", sa.String(), nullable=False),
        sa.Column("reviewer_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ad_request_id"], ["ad_request.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_suggested_post_review_idempotency"),
    )
    op.create_index(
        "ix_suggested_post_review_request_reviewed",
        "suggested_post_review",
        ["ad_request_id", "reviewed_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_suggested_post_review_request_reviewed",
        table_name="suggested_post_review",
    )
    op.drop_table("suggested_post_review")
