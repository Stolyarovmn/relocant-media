"""add append-only source revision decisions

Revision ID: 0010_source_revision_decisions
Revises: 0009_editorial_experiments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_source_revision_decisions"
down_revision: str | None = "0009_editorial_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_revision_decision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["revision_id"], ["source_revision.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_source_revision_decision_idempotency",
        ),
    )
    op.create_index(
        "ix_source_revision_decision_revision_decided",
        "source_revision_decision",
        ["revision_id", "decided_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_revision_decision_revision_decided",
        table_name="source_revision_decision",
    )
    op.drop_table("source_revision_decision")
