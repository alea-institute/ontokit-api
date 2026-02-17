"""Add ontology_file_path, sync_status, sync_error to github_integrations.

Revision ID: i7d8e9f0g1h2
Revises: h6c7d8e9f0g1
Create Date: 2026-02-17

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "i7d8e9f0g1h2"
down_revision: str | None = "h6c7d8e9f0g1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "github_integrations",
        sa.Column("ontology_file_path", sa.String(500), nullable=True),
    )
    op.add_column(
        "github_integrations",
        sa.Column("sync_status", sa.String(50), server_default="idle", nullable=False),
    )
    op.add_column(
        "github_integrations",
        sa.Column("sync_error", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("github_integrations", "sync_error")
    op.drop_column("github_integrations", "sync_status")
    op.drop_column("github_integrations", "ontology_file_path")
