"""add subject_type to lint_issues

Revision ID: 47cc27515626
Revises: 94afeba9ab5c
Create Date: 2026-04-21
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "47cc27515626"
down_revision = "94afeba9ab5c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lint_issues", sa.Column("subject_type", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("lint_issues", "subject_type")
