"""Cap active embedding jobs per project.

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "z3a4b5c6d7e8"
down_revision: str | None = "y2z3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_embedding_job_active_project",
        "embedding_jobs",
        ["project_id"],
        unique=True,
        postgresql_where="status IN ('pending', 'running')",
    )


def downgrade() -> None:
    op.drop_index("uq_embedding_job_active_project", table_name="embedding_jobs")
