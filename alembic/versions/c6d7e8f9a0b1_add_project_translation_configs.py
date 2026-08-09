"""Add per-project translation configuration.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_translation_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("language_tags", sa.JSON(), nullable=False),
        sa.Column("verification_mechanism", sa.String(length=20), nullable=False),
        sa.Column("consensus_threshold", sa.Float(), nullable=False),
        sa.Column("confidence_threshold", sa.Float(), nullable=False),
        sa.Column("translate_definitions", sa.Boolean(), nullable=False),
        sa.Column("translate_examples", sa.Boolean(), nullable=False),
        sa.Column("speed_mode", sa.String(length=20), nullable=False),
        sa.Column("provisional_gate", sa.Boolean(), nullable=False),
        sa.Column("verifier_provider", sa.String(length=50), nullable=True),
        sa.Column("verifier_model", sa.String(length=200), nullable=True),
        sa.Column("verifier_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "verification_mechanism IN ('consensus', 'confidence')",
            name="ck_project_translation_verification_mechanism",
        ),
        sa.CheckConstraint(
            "speed_mode IN ('batch', 'fast')", name="ck_project_translation_speed_mode"
        ),
        sa.CheckConstraint(
            "consensus_threshold >= 0 AND consensus_threshold <= 1",
            name="ck_project_translation_consensus_threshold",
        ),
        sa.CheckConstraint(
            "confidence_threshold >= 0 AND confidence_threshold <= 1",
            name="ck_project_translation_confidence_threshold",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("project_translation_configs")
