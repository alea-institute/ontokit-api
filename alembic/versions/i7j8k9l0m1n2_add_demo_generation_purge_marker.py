"""Add durable purge markers to demo generations.

Revision ID: i7j8k9l0m1n2
Revises: h6i7j8k9l0m1
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "i7j8k9l0m1n2"
down_revision: str | None = "h6i7j8k9l0m1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "demo_generations", sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("demo_generations", sa.Column("purge_receipt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("demo_generations", "purge_receipt")
    op.drop_column("demo_generations", "purged_at")
