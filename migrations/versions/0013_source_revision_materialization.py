"""add source revision materialization lineage

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_revision_materialization",
        sa.Column("revision_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_item_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_raw_item_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["parent_raw_item_id"], ["raw_item.id"]),
        sa.ForeignKeyConstraint(["raw_item_id"], ["raw_item.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["source_revision.id"]),
        sa.PrimaryKeyConstraint("revision_id"),
        sa.UniqueConstraint(
            "raw_item_id",
            name="uq_source_revision_materialization_raw_item",
        ),
    )


def downgrade() -> None:
    op.drop_table("source_revision_materialization")
