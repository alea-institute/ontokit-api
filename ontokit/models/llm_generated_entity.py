"""Server-owned ledger of classes proposed by the project LLM."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ontokit.core.database import Base


class LLMGeneratedEntity(Base):
    """One proposal returned by a child or sibling generation request."""

    __tablename__ = "llm_generated_entities"
    __table_args__ = (
        Index("ix_llm_generated_project_iri", "project_id", "entity_iri"),
        Index("ix_llm_generated_project_batch", "project_id", "batch_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(255))
    entity_iri: Mapped[str] = mapped_column(String(2000))
    batch_id: Mapped[UUID]
    suggestion_type: Mapped[str] = mapped_column(String(20))
    focus_iri: Mapped[str] = mapped_column(String(2000))
    label: Mapped[str] = mapped_column(Text)
    definition: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_template: Mapped[str | None] = mapped_column(String(50))
    user_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
