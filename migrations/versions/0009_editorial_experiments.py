"""Add bounded editorial experiment registry and immutable assignments."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "editorial_experiment",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="draft"),
        sa.Column("scope", sa.String(), nullable=False, server_default="presentation"),
        sa.Column("control_rule_version", sa.String(), nullable=False),
        sa.Column("treatment_rule_version", sa.String(), nullable=False),
        sa.Column("eligibility_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligibility_end", sa.DateTime(timezone=True)),
        sa.Column("success_metric", sa.String(), nullable=False),
        sa.Column("horizon_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("minimum_samples_per_arm", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("noise_floor", sa.Numeric(8, 6), nullable=False, server_default="0.02"),
        sa.Column(
            "max_single_post_view_share",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="0.5",
        ),
        sa.Column("rollback_rule_version", sa.String()),
        sa.Column("rollback_marked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "experiment_assignment",
        sa.Column(
            "experiment_id",
            sa.String(),
            sa.ForeignKey("editorial_experiment.id"),
            primary_key=True,
        ),
        sa.Column(
            "raw_item_id",
            sa.BigInteger(),
            sa.ForeignKey("raw_item.id"),
            primary_key=True,
        ),
        sa.Column("post_id", sa.BigInteger(), sa.ForeignKey("post.id"), unique=True),
        sa.Column("arm", sa.String(), nullable=False),
        sa.Column("rule_version", sa.String(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("experiment_assignment")
    op.drop_table("editorial_experiment")
