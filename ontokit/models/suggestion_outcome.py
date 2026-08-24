"""Append-only suggestion outcome log (trust ladder, R5).

Every terminal review action on a suggestion session — accepted, rejected, or
dismissed — appends exactly one row here. Rows are NEVER updated or deleted:
the log is the single source of truth for promotion counting (R6) and the
substrate the future contributor-standing UI reads without needing a migration.

`counts_toward_promotion` encodes R7 ("anonymous contributions are credited but
never counted") as a data property rather than a query-site convention, so a new
call site cannot silently start counting anonymous work.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from ontokit.models.project import Project

from ontokit.core.database import Base


class SuggestionOutcomeType(StrEnum):
    """Terminal outcomes a suggestion session can reach."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DISMISSED = "dismissed"


class SuggestionOutcome(Base):
    """One append-only record of a suggestion's terminal review outcome."""

    __tablename__ = "suggestion_outcomes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Zitadel user ID for authenticated contributors; the anonymous pseudo-user
    # ID (``anonymous-<hex>``) for anonymous sessions.
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Nullable + SET NULL: the log must outlive session cleanup (append-only).
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suggestion_sessions.id", ondelete="SET NULL"), nullable=True
    )

    outcome: Mapped[str] = mapped_column(String(20), nullable=False)

    # R7: anonymous work is credited but never counted toward promotion.
    counts_toward_promotion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_anonymous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Who decided. A human reviewer's user ID, or "system:auto-accept".
    decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship()

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_suggestion_outcome_session"),
        Index("ix_suggestion_outcomes_project_user", "project_id", "user_id"),
        # The only hot query is the promotion count; keep it a partial index.
        Index(
            "ix_suggestion_outcomes_promotion",
            "project_id",
            "user_id",
            postgresql_where=text("outcome = 'accepted' AND counts_toward_promotion"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SuggestionOutcome(project_id={self.project_id}, user_id={self.user_id!r}, "
            f"outcome={self.outcome!r}, counts={self.counts_toward_promotion})>"
        )
