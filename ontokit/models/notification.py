"""Notification database model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from ontokit.core.database import Base


class Notification(Base):
    """Per-user notification for project events."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # KTD20: nullable so notifications that belong to no OntoKit project (e.g.
    # PR Party's `pr_party_ready`) can share this table and the one bell
    # endpoint. Project-scoped notifications still always set both.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    project_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    is_read: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index(
            "ix_notifications_user_unread_created",
            "user_id",
            "is_read",
            created_at.desc(),
        ),
        # R22/KTD20: one `pr_party_ready` per reviewer per PR revision. The
        # target_id is `{repo_full_name}#{pr_number}:{head_sha}`, so a re-push
        # notifies again while a re-sweep of the same revision cannot.
        # Partial, so it constrains only PR Party rows.
        Index(
            "uq_notification_pr_party_ready",
            "user_id",
            "type",
            "target_id",
            unique=True,
            postgresql_where=text("type = 'pr_party_ready'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Notification(id={self.id}, user_id={self.user_id!r}, "
            f"type={self.type!r}, is_read={self.is_read})>"
        )
