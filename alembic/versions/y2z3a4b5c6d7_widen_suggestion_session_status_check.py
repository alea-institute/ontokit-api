"""Widen suggestion session status CHECK to match review actions.

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "y2z3a4b5c6d7"
down_revision: str | None = "x1y2z3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_STATUSES = (
    "status IN ('active', 'submitted', 'auto-submitted', 'discarded', "
    "'merged', 'rejected', 'changes-requested')"
)
_LEGACY_STATUSES = "status IN ('active', 'submitted', 'auto-submitted', 'discarded')"


def upgrade() -> None:
    op.drop_constraint(
        "ck_suggestion_session_status", "suggestion_sessions", type_="check"
    )
    op.create_check_constraint(
        "ck_suggestion_session_status", "suggestion_sessions", _ALL_STATUSES
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_suggestion_session_status", "suggestion_sessions", type_="check"
    )
    op.create_check_constraint(
        "ck_suggestion_session_status", "suggestion_sessions", _LEGACY_STATUSES
    )
