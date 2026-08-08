"""Suggestion session database model."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from ontokit.models.project import Project
    from ontokit.models.pull_request import PullRequest

from ontokit.core.database import Base


class SuggestionSessionStatus(StrEnum):
    """Suggestion session status enum."""

    ACTIVE = "active"
    SUBMITTED = "submitted"
    AUTO_SUBMITTED = "auto-submitted"
    DISCARDED = "discarded"
    MERGED = "merged"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes-requested"


class SuggestionSession(Base):
    """Suggestion session model for tracking suggester edits."""

    __tablename__ = "suggestion_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    # User info
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Session identifiers
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)

    # Status and progress
    status: Mapped[str] = mapped_column(String(50), default=SuggestionSessionStatus.ACTIVE.value)
    changes_count: Mapped[int] = mapped_column(Integer, default=0)
    entities_modified: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Auth
    beacon_token: Mapped[str] = mapped_column(String(500), nullable=False)

    # Anonymous session fields
    is_anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    submitter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitter_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # --- Trust ladder fields (R10, R11, R12, R13) ---
    # LLM-generated suggestions are NEVER auto-accepted, at any tier (R13/KD5).
    is_llm_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # When set, the session is eligible to auto-merge at or after this instant.
    # NULL means "not scheduled" — never scheduled, or halted by an objection.
    auto_accept_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Stamped when a reviewer objection halts the clock (R12).
    auto_accept_halted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # First-suggestion human-verification challenge (R10); persisted so a retry
    # after a network blip does not re-challenge the contributor.
    verification_passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # PR link (set after submit)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pr_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="SET NULL"), nullable=True
    )

    # Review fields
    reviewer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewer_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    last_activity: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    project: Mapped["Project"] = relationship()
    pull_request: Mapped["PullRequest | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("project_id", "session_id", name="uq_suggestion_session"),
        CheckConstraint(
            "status IN ('active', 'submitted', 'auto-submitted', 'discarded', "
            "'merged', 'rejected', 'changes-requested')",
            name="ck_suggestion_session_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SuggestionSession(id={self.id}, project_id={self.project_id}, "
            f"session_id={self.session_id!r}, status={self.status!r})>"
        )
