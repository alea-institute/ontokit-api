"""SQLAlchemy model for auditable distinct-entity decisions."""

__all__ = ["DuplicateRejection"]

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ontokit.core.database import Base


class DuplicateRejection(Base):
    """Records that a reviewer marked a canonical IRI pair as distinct.

    Rows are immutable audit snapshots except for revocation/supersession fields.
    Fingerprints bind the decision to the normalized detector inputs that were
    reviewed, so changed entity content resurfaces automatically.
    """

    __tablename__ = "duplicate_rejections"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    iri_a: Mapped[str] = mapped_column(String(2000), nullable=False)
    iri_b: Mapped[str] = mapped_column(String(2000), nullable=False)
    fingerprint_a: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint_b: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    marked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    marked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    suggestion_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suggestion_sessions.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("duplicate_rejections.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        CheckConstraint("iri_a < iri_b", name="ck_duplicate_rejections_canonical_pair"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_duplicate_rejections_reason"),
        Index(
            "uq_duplicate_rejections_active_pair",
            "project_id",
            "iri_a",
            "iri_b",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
            sqlite_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_duplicate_rejections_history",
            "project_id",
            "iri_a",
            "iri_b",
            "marked_at",
        ),
    )
