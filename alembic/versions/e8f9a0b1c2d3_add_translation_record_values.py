"""Add review-queue values to translation records.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("translation_records", sa.Column("source_value", sa.Text(), nullable=True))
    op.add_column("translation_records", sa.Column("proposed_value", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("translation_records", "proposed_value")
    op.drop_column("translation_records", "source_value")
