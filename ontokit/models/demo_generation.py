"""Auditable lifecycle records for atomically published demo generations."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ontokit.core.database import Base


class DemoGenerationStatus(StrEnum):
    """Lifecycle states for one coherent pair of demo snapshots."""

    PREPARING = "preparing"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRED = "retired"


class DemoGeneration(Base):
    """A complete, immutable-by-identity demo publication candidate."""

    __tablename__ = "demo_generations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    generation_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DemoGenerationStatus.PREPARING.value,
        server_default=DemoGenerationStatus.PREPARING.value,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    attempt_token: Mapped[uuid.UUID] = mapped_column(
        nullable=False,
        default=uuid.uuid4,
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    projects: Mapped[list[Project]] = relationship(back_populates="demo_generation")

    __table_args__ = (
        CheckConstraint(
            "status IN ('preparing', 'active', 'failed', 'retired')",
            name="ck_demo_generations_status",
        ),
        Index(
            "uq_demo_generations_single_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


from ontokit.models.project import Project  # noqa: E402
