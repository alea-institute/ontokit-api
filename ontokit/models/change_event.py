"""Entity change event model for tracking ontology modifications."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, FetchedValue, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ontokit.core.database import Base

MAX_ENTITY_IRI_LENGTH = 2000


class ChangeEventType(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    RENAME = "rename"
    REPARENT = "reparent"
    DEPRECATE = "deprecate"


class ChangeOrigin(StrEnum):
    HUMAN = "human"
    AI = "ai"
    AI_EDITED = "ai_edited"
    SYSTEM = "system"


class EntityChangeEvent(Base):
    __tablename__ = "entity_change_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    branch: Mapped[str] = mapped_column(String(255))
    entity_iri: Mapped[str] = mapped_column(String(MAX_ENTITY_IRI_LENGTH))
    entity_type: Mapped[str] = mapped_column(String(50))
    event_type: Mapped[str] = mapped_column(String(50))
    user_id: Mapped[str] = mapped_column(String(255))
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_hash: Mapped[str | None] = mapped_column(String(40), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), server_default=ChangeOrigin.HUMAN)
    origin_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    origin_batch_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    changed_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    old_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    new_values: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The PostgreSQL transaction that inserted the event, filled by the database.
    # Events from before the column existed carry NULL. Feed cursors compare it
    # with a snapshot to page through one fixed set of events.
    recorded_xact: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, server_default=FetchedValue()
    )

    project: Mapped["Project"] = relationship()  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        Index("ix_change_events_project_entity", "project_id", "entity_iri"),
        Index("ix_change_events_project_time", "project_id", "created_at"),
        Index("ix_change_events_project_branch_time", "project_id", "branch", "created_at"),
        Index("ix_change_events_project_origin_time", "project_id", "origin", "created_at"),
        Index("ix_change_events_project_branch_commit", "project_id", "branch", "commit_hash"),
        Index(
            "ix_change_events_project_branch_entity_time",
            "project_id",
            "branch",
            "entity_iri",
            "created_at",
            "id",
            postgresql_include=["event_type", "origin", "recorded_xact"],
        ),
    )
