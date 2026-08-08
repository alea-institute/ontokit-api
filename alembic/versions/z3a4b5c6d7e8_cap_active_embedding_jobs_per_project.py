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
    op.execute("""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY project_id
                ORDER BY (status = 'running') DESC, started_at DESC, id DESC
            ) AS active_rank
            FROM embedding_jobs
            WHERE status IN ('pending', 'running')
        )
        UPDATE embedding_jobs AS jobs
        SET status = 'failed',
            error_message = 'Superseded while enforcing one active job per project',
            completed_at = now()
        FROM ranked
        WHERE jobs.id = ranked.id AND ranked.active_rank > 1
    """)
    op.create_index(
        "uq_embedding_job_active_project",
        "embedding_jobs",
        ["project_id"],
        unique=True,
        postgresql_where="status IN ('pending', 'running')",
    )


def downgrade() -> None:
    op.drop_index("uq_embedding_job_active_project", table_name="embedding_jobs")
