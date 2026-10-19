"""Persist source policy, event fingerprints and editorial rule provenance."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source",
        sa.Column("ownership", sa.String(), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "source",
        sa.Column("high_risk", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "source",
        sa.Column("auto_publish", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column("raw_item", sa.Column("canonical_url", sa.Text()))
    op.add_column("raw_item", sa.Column("content_sha256", sa.String(length=64)))
    op.create_index("ix_raw_item_canonical_url", "raw_item", ["canonical_url"])
    op.create_index("ix_raw_item_content_sha256", "raw_item", ["content_sha256"])

    # Existing rows predate versioned editorial rules. New ORM writes explicitly use
    # the current version; the server default preserves honest provenance for legacy rows.
    op.add_column(
        "post",
        sa.Column(
            "editorial_rule_version",
            sa.String(),
            nullable=False,
            server_default="legacy-v1",
        ),
    )
    op.add_column(
        "editorial_run",
        sa.Column(
            "editorial_rule_version",
            sa.String(),
            nullable=False,
            server_default="legacy-v1",
        ),
    )


def downgrade() -> None:
    op.drop_column("editorial_run", "editorial_rule_version")
    op.drop_column("post", "editorial_rule_version")
    op.drop_index("ix_raw_item_content_sha256", table_name="raw_item")
    op.drop_index("ix_raw_item_canonical_url", table_name="raw_item")
    op.drop_column("raw_item", "content_sha256")
    op.drop_column("raw_item", "canonical_url")
    op.drop_column("source", "auto_publish")
    op.drop_column("source", "high_risk")
    op.drop_column("source", "ownership")
