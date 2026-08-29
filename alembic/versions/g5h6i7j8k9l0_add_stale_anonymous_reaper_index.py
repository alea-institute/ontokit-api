"""Add the stale-anonymous reaper index without blocking writers.

Revision ID: g5h6i7j8k9l0
Revises: f4g5h6i7j8k9
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g5h6i7j8k9l0"
down_revision: str | None = "f4g5h6i7j8k9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_suggestion_sessions_stale_anonymous",
            "suggestion_sessions",
            ["last_activity", "id"],
            unique=False,
            postgresql_where=sa.text("status = 'active' AND is_anonymous IS true"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_suggestion_sessions_stale_anonymous",
            table_name="suggestion_sessions",
            postgresql_concurrently=True,
        )
