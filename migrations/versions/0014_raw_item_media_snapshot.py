"""add raw item media provenance snapshots

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_value = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "raw_item_media_snapshot",
        sa.Column("raw_item_id", sa.BigInteger(), nullable=False),
        sa.Column("source_page", sa.Text(), nullable=False),
        sa.Column("source_html", sa.Text(), nullable=True),
        sa.Column("source_html_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_html_bytes", sa.Integer(), nullable=False),
        sa.Column("manifest", json_value, nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["raw_item_id"],
            ["raw_item.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("raw_item_id"),
    )


def downgrade() -> None:
    op.drop_table("raw_item_media_snapshot")
