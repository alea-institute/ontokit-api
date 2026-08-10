"""Append-only suggestion outcome log (trust ladder, R5).

Every terminal review action on a suggestion session — accepted, rejected, or
dismissed — appends exactly one row here. Rows are never deleted, and their
standing snapshot is never updated. The only permitted mutation is clearing the
three display-attribution fields for an authorized erasure or abuse takedown.
The log is the single source of truth for promotion counting (R6) and the
substrate the contributor-standing UI reads.

`counts_toward_promotion` encodes R7 ("anonymous contributions are credited but
never counted") as a data property rather than a query-site convention, so a new
call site cannot silently start counting anonymous work.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, text
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

    # Standing and attribution captured when this terminal outcome was decided.
    # NULL means the row predates snapshot capture; no defaults may fabricate history.
    # Display fields remain nullable so authorized erasure/takedown requests can clear them.
    snapshot_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    snapshot_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    submitter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitter_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    snapshot_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship()

    __table_args__ = (
        Index("ix_suggestion_outcomes_project_user", "project_id", "user_id"),
        # The only hot query is the promotion count; keep it a partial index.
        Index(
            "ix_suggestion_outcomes_promotion",
            "project_id",
            "user_id",
            postgresql_where=text("outcome = 'accepted' AND counts_toward_promotion"),
        ),
        Index(
            "ix_suggestion_outcomes_audit_cursor",
            project_id.desc(),
            created_at.desc(),
            id.desc(),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SuggestionOutcome(project_id={self.project_id}, user_id={self.user_id!r}, "
            f"outcome={self.outcome!r}, counts={self.counts_toward_promotion})>"
        )
