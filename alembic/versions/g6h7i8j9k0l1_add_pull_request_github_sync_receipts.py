"""Add visible GitHub pull-request mirror receipts.

Revision ID: g6h7i8j9k0l1
Revises: e4f5g6h7i8j9
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "g6h7i8j9k0l1"
down_revision = "e4f5g6h7i8j9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pull_requests",
        sa.Column(
            "github_sync_status",
            sa.String(length=30),
            server_default="not_configured",
            nullable=False,
        ),
    )
    op.add_column(
        "pull_requests",
        sa.Column("github_sync_last_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pull_requests", sa.Column("github_sync_message", sa.Text(), nullable=True)
    )
    op.add_column(
        "pull_requests", sa.Column("github_sync_attempt_id", sa.UUID(), nullable=True)
    )
    op.create_check_constraint(
        "ck_pull_requests_github_sync_status",
        "pull_requests",
        "github_sync_status IN ('not_configured', 'pending', 'synced', 'failed')",
    )
    op.execute(
        """
        UPDATE pull_requests
        SET github_sync_status = 'synced'
        WHERE github_pr_number IS NOT NULL OR github_pr_url IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_pull_requests_github_sync_status", "pull_requests", type_="check"
    )
    op.drop_column("pull_requests", "github_sync_attempt_id")
    op.drop_column("pull_requests", "github_sync_message")
    op.drop_column("pull_requests", "github_sync_last_attempted_at")
    op.drop_column("pull_requests", "github_sync_status")
