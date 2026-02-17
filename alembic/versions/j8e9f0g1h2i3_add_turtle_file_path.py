"""Add turtle_file_path to github_integrations.

Revision ID: j8e9f0g1h2i3
Revises: i7d8e9f0g1h2
Create Date: 2026-02-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "j8e9f0g1h2i3"
down_revision: str | None = "i7d8e9f0g1h2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "github_integrations",
        sa.Column("turtle_file_path", sa.String(500), nullable=True),
    )
    # Backfill: when ontology_file_path is already .ttl, set turtle_file_path to match
    op.execute(
        "UPDATE github_integrations "
        "SET turtle_file_path = ontology_file_path "
        "WHERE ontology_file_path LIKE '%.ttl'"
    )


def downgrade() -> None:
    op.drop_column("github_integrations", "turtle_file_path")
