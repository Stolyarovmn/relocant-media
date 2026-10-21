"""add append-only reader correction decisions

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reader_correction_decision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("submission_id", sa.BigInteger(), nullable=False),
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
        sa.ForeignKeyConstraint(["submission_id"], ["submission.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_reader_correction_decision_idempotency",
        ),
    )
    op.create_index(
        "ix_reader_correction_decision_submission_decided",
        "reader_correction_decision",
        ["submission_id", "decided_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reader_correction_decision_submission_decided",
        table_name="reader_correction_decision",
    )
    op.drop_table("reader_correction_decision")
