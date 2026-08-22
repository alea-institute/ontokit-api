"""Add one-open-PR-per-source-branch invariant.

Revision ID: e4f5g6h7i8j9
Revises: d3e4f5g6h7i8
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5g6h7i8j9"
down_revision: str | None = "d3e4f5g6h7i8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_pull_requests_open_source_branch",
        "pull_requests",
        ["project_id", "source_branch"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_pull_requests_open_source_branch",
        table_name="pull_requests",
    )
