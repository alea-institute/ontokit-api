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
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT project_id, source_branch, COUNT(*) AS duplicate_count
            FROM pull_requests
            WHERE status = 'open'
            GROUP BY project_id, source_branch
            HAVING COUNT(*) > 1
            ORDER BY project_id, source_branch
            """
            )
        )
        .all()
    )
    if duplicates:
        groups = ", ".join(
            f"{project_id}/{source_branch} ({count})"
            for project_id, source_branch, count in duplicates
        )
        raise RuntimeError(
            "Cannot enforce one open pull request per source branch; "
            f"resolve duplicate groups first: {groups}"
        )

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
