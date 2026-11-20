"""Add change origins and the generated entity ledger.

Revision ID: j8k9l0m1n2o3
Revises: i7j8k9l0m1n2
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "j8k9l0m1n2o3"
down_revision: str | None = "i7j8k9l0m1n2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The concurrent index build below commits these columns before the
    # revision is recorded. If that build is interrupted, the rerun finds the
    # columns and an invalid index, so both steps tolerate their own leftovers.
    op.add_column(
        "entity_change_events",
        sa.Column("origin", sa.String(20), nullable=False, server_default="human"),
        if_not_exists=True,
    )
    op.add_column(
        "entity_change_events",
        sa.Column("origin_model", sa.String(255), nullable=True),
        if_not_exists=True,
    )
    op.add_column(
        "entity_change_events",
        sa.Column("origin_batch_id", sa.Uuid(), nullable=True),
        if_not_exists=True,
    )
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_change_events_project_origin_time",
            table_name="entity_change_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            "ix_change_events_project_origin_time",
            "entity_change_events",
            ["project_id", "origin", "created_at"],
            postgresql_concurrently=True,
        )

    op.create_table(
        "llm_generated_entities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("entity_iri", sa.String(2000), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("suggestion_type", sa.String(20), nullable=False),
        sa.Column("focus_iri", sa.String(2000), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("prompt_template", sa.String(50), nullable=True),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_llm_generated_project_iri", "llm_generated_entities", ["project_id", "entity_iri"]
    )
    op.create_index(
        "ix_llm_generated_project_batch", "llm_generated_entities", ["project_id", "batch_id"]
    )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        # The drop commits on its own, so a rerun may find the index gone.
        op.drop_index(
            "ix_change_events_project_origin_time",
            table_name="entity_change_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_index("ix_llm_generated_project_batch", table_name="llm_generated_entities")
    op.drop_index("ix_llm_generated_project_iri", table_name="llm_generated_entities")
    op.drop_table("llm_generated_entities")
    op.drop_column("entity_change_events", "origin_batch_id")
    op.drop_column("entity_change_events", "origin_model")
    op.drop_column("entity_change_events", "origin")
