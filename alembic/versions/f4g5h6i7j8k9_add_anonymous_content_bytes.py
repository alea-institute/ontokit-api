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
    op.create_index(
        "ix_suggestion_sessions_stale_anonymous",
        "suggestion_sessions",
        ["last_activity", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'active' AND is_anonymous IS true"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_suggestion_sessions_stale_anonymous",
        table_name="suggestion_sessions",
    )
    op.drop_column("suggestion_sessions", "anonymous_content_bytes")
