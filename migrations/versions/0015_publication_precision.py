"""Retain explicit publication precision without guessing legacy timestamps.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("raw_item", sa.Column(
        "published_at_precision", sa.String(), nullable=False, server_default="unknown",
    ))


def downgrade() -> None:
    op.drop_column("raw_item", "published_at_precision")
