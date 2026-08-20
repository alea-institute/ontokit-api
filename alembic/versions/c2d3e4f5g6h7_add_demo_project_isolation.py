"""Add fail-closed demo project identity and source linkage.

Revision ID: c2d3e4f5g6h7
Revises: b1c2d3e4f5g6
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2d3e4f5g6h7"
down_revision: str | None = "b1c2d3e4f5g6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "projects", sa.Column("demo_source_project_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_projects_demo_source_project_id",
        "projects",
        "projects",
        ["demo_source_project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_projects_demo_source_project_id", "projects", ["demo_source_project_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_projects_demo_source_project_id", "projects", type_="unique")
    op.drop_constraint("fk_projects_demo_source_project_id", "projects", type_="foreignkey")
    op.drop_column("projects", "demo_source_project_id")
    op.drop_column("projects", "is_demo")
