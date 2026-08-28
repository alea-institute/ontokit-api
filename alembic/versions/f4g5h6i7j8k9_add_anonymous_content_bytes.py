"""Track cumulative anonymous suggestion content bytes.

Revision ID: f4g5h6i7j8k9
Revises: e3f4g5h6i7j8
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4g5h6i7j8k9"
down_revision: str | None = "e3f4g5h6i7j8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "suggestion_sessions",
        sa.Column(
            "anonymous_content_bytes",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("suggestion_sessions", "anonymous_content_bytes")
