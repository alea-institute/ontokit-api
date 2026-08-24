"""Expand the suggestion-session status constraint for reviewed outcomes.

Revision ID: e3f4g5h6i7j8
Revises: d2e3f4g5h6i7
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3f4g5h6i7j8"
down_revision: str | None = "d2e3f4g5h6i7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGINAL_STATUSES = ("active", "submitted", "auto-submitted", "discarded")
_REVIEWED_STATUSES = ("merged", "rejected", "changes-requested")


def _status_check(statuses: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{status}'" for status in statuses)
    return f"status IN ({quoted})"


def upgrade() -> None:
    op.drop_constraint(
        "ck_suggestion_session_status",
        "suggestion_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_suggestion_session_status",
        "suggestion_sessions",
        _status_check(_ORIGINAL_STATUSES + _REVIEWED_STATUSES),
    )


def downgrade() -> None:
    reviewed_rows_exist = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM suggestion_sessions
                    WHERE status IN ('merged', 'rejected', 'changes-requested')
                )
                """
            )
        )
        .scalar_one()
    )
    if reviewed_rows_exist:
        raise RuntimeError(
            "Cannot restore the original suggestion-session status constraint while "
            "reviewed terminal rows exist"
        )

    op.drop_constraint(
        "ck_suggestion_session_status",
        "suggestion_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_suggestion_session_status",
        "suggestion_sessions",
        _status_check(_ORIGINAL_STATUSES),
    )
