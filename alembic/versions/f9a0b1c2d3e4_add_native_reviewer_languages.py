"""Add per-language native reviewer tags.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9a0b1c2d3e4"
down_revision: str | None = "e8f9a0b1c2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "native_reviewer_languages",
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.ForeignKeyConstraint(["member_id"], ["project_members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("member_id", "language"),
    )


def downgrade() -> None:
    op.drop_table("native_reviewer_languages")
