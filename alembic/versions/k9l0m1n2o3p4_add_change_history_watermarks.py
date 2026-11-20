"""Add branch watermarks and index events by commit hash.

Revision ID: k9l0m1n2o3p4
Revises: j8k9l0m1n2o3
"""

import sqlalchemy as sa

from alembic import op

revision: str = "k9l0m1n2o3p4"
down_revision: str | None = "j8k9l0m1n2o3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "change_history_watermarks",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE")),
        sa.Column("branch", sa.String(255)),
        sa.Column("commit_hash", sa.String(40), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("project_id", "branch"),
    )
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_change_events_project_branch_commit",
            "entity_change_events",
            ["project_id", "branch", "commit_hash"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_change_events_project_branch_commit",
            table_name="entity_change_events",
            postgresql_concurrently=True,
        )
    op.drop_table("change_history_watermarks")
