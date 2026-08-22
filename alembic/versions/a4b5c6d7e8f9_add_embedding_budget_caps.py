"""Add embedding-specific budget caps.

Revision ID: a4b5c6d7e8f9
Revises: z3a4b5c6d7e8
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: str | None = "z3a4b5c6d7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_embedding_configs", sa.Column("monthly_budget_usd", sa.Float()))
    op.add_column("project_embedding_configs", sa.Column("daily_cap_usd", sa.Float()))


def downgrade() -> None:
    op.drop_column("project_embedding_configs", "daily_cap_usd")
    op.drop_column("project_embedding_configs", "monthly_budget_usd")
