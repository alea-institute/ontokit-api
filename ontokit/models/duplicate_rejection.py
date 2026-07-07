"""SQLAlchemy model for duplicate rejection records."""

__all__ = ["DuplicateRejection"]

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ontokit.core.database import Base


class DuplicateRejection(Base):
    """Records a human decision that two IRIs are NOT duplicates, or that one should be rejected.

    When a reviewer dismisses a duplicate warning (iri A is NOT a duplicate of canonical B),
    that rejection is stored here so future checks can skip the pair.
    """

    __tablename__ = "duplicate_rejections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    rejected_iri: Mapped[str] = mapped_column(String(2000))
    canonical_iri: Mapped[str] = mapped_column(String(2000))
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_by: Mapped[str] = mapped_column(String(255))
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    suggestion_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suggestion_sessions.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_duplicate_rejections_lookup", "project_id", "rejected_iri"),
    )
