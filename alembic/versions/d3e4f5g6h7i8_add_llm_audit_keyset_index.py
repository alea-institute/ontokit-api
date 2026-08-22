"""add llm audit keyset index

Revision ID: d3e4f5g6h7i8
Revises: c2d3e4f5g6h7
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d3e4f5g6h7i8"
down_revision: str | None = "c2d3e4f5g6h7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_llm_audit_project_date", table_name="llm_audit_logs")
    op.create_index(
        "ix_llm_audit_project_date",
        "llm_audit_logs",
        ["project_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_audit_project_date", table_name="llm_audit_logs")
    op.create_index(
        "ix_llm_audit_project_date",
        "llm_audit_logs",
        ["project_id", "created_at"],
    )
