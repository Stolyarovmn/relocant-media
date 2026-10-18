"""Persist editorial evidence, outcomes and conservative staging delivery receipts."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "editorial_run",
        sa.Column("raw_item_id", sa.BigInteger(), sa.ForeignKey("raw_item.id"), primary_key=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("post.id"), unique=True),
        sa.Column("delivery_status", sa.String(), nullable=False),
        sa.Column("staging_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("editorial_run")
