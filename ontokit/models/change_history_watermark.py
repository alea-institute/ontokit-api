"""Progress of change-history reconciliation for a Git branch."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ontokit.core.database import Base


class ChangeHistoryWatermark(Base):
    __tablename__ = "change_history_watermarks"

    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    branch: Mapped[str] = mapped_column(String(255), primary_key=True)
    commit_hash: Mapped[str] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
