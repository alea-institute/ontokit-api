"""Add primary translation provider and model.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: str | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "project_translation_configs",
        sa.Column("primary_provider", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "project_translation_configs",
        sa.Column("primary_model", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_translation_configs", "primary_model")
    op.drop_column("project_translation_configs", "primary_provider")
