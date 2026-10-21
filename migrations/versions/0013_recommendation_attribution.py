"""add recommendation proposal attribution

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_proposal",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("recommendation_code", sa.String(), nullable=False),
        sa.Column("action_class", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("github_issue_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_recommendation_proposal_fingerprint"),
    )
    op.create_index(
        "ix_recommendation_proposal_city_date",
        "recommendation_proposal",
        ["city", "report_date", "id"],
        unique=False,
    )
    op.create_table(
        "recommendation_target_link",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("target_repo", sa.String(), nullable=False),
        sa.Column("target_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["proposal_id"], ["recommendation_proposal.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_recommendation_target_link_idempotency",
        ),
        sa.UniqueConstraint(
            "proposal_id",
            "target_kind",
            "target_repo",
            "target_number",
            name="uq_recommendation_target_link_target",
        ),
    )


def downgrade() -> None:
    op.drop_table("recommendation_target_link")
    op.drop_index(
        "ix_recommendation_proposal_city_date",
        table_name="recommendation_proposal",
    )
    op.drop_table("recommendation_proposal")
