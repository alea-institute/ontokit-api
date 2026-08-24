"""Add one-open-PR-per-source-branch invariant.

Revision ID: e4f5g6h7i8j9
Revises: x1y2z3a4b5c6
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5g6h7i8j9"
down_revision: str | None = "x1y2z3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DUPLICATE_REPORT_LIMIT = 20


def upgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                """
                WITH duplicate_groups AS (
                    SELECT project_id, source_branch, COUNT(*) AS duplicate_count
                    FROM pull_requests
                    WHERE status = 'open'
                    GROUP BY project_id, source_branch
                    HAVING COUNT(*) > 1
                )
                SELECT
                    project_id,
                    source_branch,
                    duplicate_count,
                    COUNT(*) OVER () AS total_groups
                FROM duplicate_groups
                ORDER BY project_id, source_branch
                LIMIT :report_limit
                """
            ),
            {"report_limit": DUPLICATE_REPORT_LIMIT},
        )
        .all()
    )
    if duplicates:
        groups = ", ".join(
            f"{project_id}/{source_branch} ({count})"
            for project_id, source_branch, count, _total in duplicates
        )
        total_groups = duplicates[0][3]
        suffix = (
            f"; showing first {len(duplicates)} of {total_groups} duplicate groups"
            if total_groups > len(duplicates)
            else ""
        )
        raise RuntimeError(
            "Cannot enforce one open pull request per source branch; "
            f"resolve duplicate groups first: {groups}{suffix}"
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
