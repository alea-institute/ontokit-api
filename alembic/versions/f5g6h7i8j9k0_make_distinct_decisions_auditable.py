"""Add auditable, fingerprint-bound distinct-entity decisions.

Revision ID: f5g6h7i8j9k0
Revises: e4f5g6h7i8j9
Create Date: 2026-08-23

The legacy ``duplicate_rejections`` table intentionally remains unchanged. It
records rejected suggestion provenance and must stay readable by both old code
and the duplicate-candidate API during a rolling deployment or rollback.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f5g6h7i8j9k0"
down_revision = "e4f5g6h7i8j9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "distinct_entity_decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("iri_a", sa.String(2000), nullable=False),
        sa.Column("iri_b", sa.String(2000), nullable=False),
        sa.Column("fingerprint_a", sa.String(64), nullable=False),
        sa.Column("fingerprint_b", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("marked_by", sa.String(255), nullable=False),
        sa.Column(
            "marked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("suggestion_session_id", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.Column("superseded_by_id", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "iri_a < iri_b",
            name="ck_distinct_entity_decisions_canonical_pair",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_distinct_entity_decisions_reason",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["suggestion_session_id"],
            ["suggestion_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["distinct_entity_decisions.id"],
            name="fk_distinct_entity_decisions_superseded_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_distinct_entity_decisions_active_pair",
        "distinct_entity_decisions",
        ["project_id", "iri_a", "iri_b"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_distinct_entity_decisions_history",
        "distinct_entity_decisions",
        ["project_id", "iri_a", "iri_b", "marked_at"],
    )
    op.create_index(
        "ix_distinct_entity_decisions_project_marked",
        "distinct_entity_decisions",
        ["project_id", "marked_at"],
    )
    op.create_index(
        "ix_distinct_entity_decisions_active_project_marked",
        "distinct_entity_decisions",
        ["project_id", "marked_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    decision_count = int(
        bind.execute(sa.text("SELECT count(*) FROM distinct_entity_decisions")).scalar_one()
    )
    if decision_count:
        raise RuntimeError(
            "Refusing to downgrade while distinct_entity_decisions contains audit data: "
            f"count={decision_count}. Export or explicitly resolve those decisions first."
        )

    op.drop_index(
        "ix_distinct_entity_decisions_history",
        table_name="distinct_entity_decisions",
    )
    op.drop_index(
        "ix_distinct_entity_decisions_project_marked",
        table_name="distinct_entity_decisions",
    )
    op.drop_index(
        "ix_distinct_entity_decisions_active_project_marked",
        table_name="distinct_entity_decisions",
    )
    op.drop_index(
        "uq_distinct_entity_decisions_active_pair",
        table_name="distinct_entity_decisions",
    )
    op.drop_table("distinct_entity_decisions")
