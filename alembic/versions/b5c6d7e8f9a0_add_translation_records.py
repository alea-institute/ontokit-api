"""Add durable machine-translation provenance records.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "translation_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("entity_iri", sa.String(length=2000), nullable=False),
        sa.Column("predicate", sa.String(length=2000), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("source_value_hash", sa.String(length=64), nullable=False),
        sa.Column("translated_value_hash", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_version", sa.String(length=200), nullable=False),
        sa.Column("method", sa.String(length=50), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("state", sa.String(length=20), server_default="provisional", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirming_member_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "state IN ('verified', 'provisional', 'rejected')",
            name="ck_translation_record_state",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["confirming_member_id"], ["project_members.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "entity_iri",
            "predicate",
            "language",
            "source_value_hash",
            "translated_value_hash",
            name="uq_translation_record_literal",
        ),
    )
    op.create_index(
        "ix_translation_records_unconfirmed_era",
        "translation_records",
        ["project_id", "created_at"],
        postgresql_where=sa.text("confirmed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_translation_records_unconfirmed_era", table_name="translation_records"
    )
    op.drop_table("translation_records")
