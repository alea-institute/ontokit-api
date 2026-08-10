"""Add audit snapshot columns to suggestion outcomes.

This migration is safe on populated tables because it is purely additive and
every new column is nullable with no server default. Existing outcomes therefore
retain an explicit NULL pre-feature snapshot rather than fabricated history.

Revision ID: b1c2d3e4f5g6
Revises: a0b1c2d3e4f5
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5g6"
down_revision: str | None = "a0b1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "suggestion_outcomes",
        sa.Column("snapshot_tier", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "suggestion_outcomes",
        sa.Column("snapshot_role", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "suggestion_outcomes",
        sa.Column("submitter_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "suggestion_outcomes",
        sa.Column("submitter_email", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "suggestion_outcomes",
        sa.Column("decided_by_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "suggestion_outcomes",
        sa.Column("snapshot_captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_suggestion_outcomes_audit_cursor",
        "suggestion_outcomes",
        [
            sa.text("project_id DESC"),
            sa.text("created_at DESC"),
            sa.text("id DESC"),
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_suggestion_outcomes_audit_cursor",
        table_name="suggestion_outcomes",
    )
    raise RuntimeError(
        "Snapshot columns contain append-only audit data and cannot be dropped automatically. "
        "Manual data-preserving rollback procedure: export and verify the six snapshot columns, "
        "then explicitly drop them only after the preserved data is secured."
    )
