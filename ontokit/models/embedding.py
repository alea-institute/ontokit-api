"""Embedding models for vector search and similarity."""

__all__ = [
    "EmbeddingJob",
    "EntityEmbedding",
    "EntityEmbeddingStaging",
    "ProjectEmbeddingConfig",
    "Vector",
]

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ontokit.core.database import Base


class ProjectEmbeddingConfig(Base):
    __tablename__ = "project_embedding_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(50), default="local")
    model_name: Mapped[str] = mapped_column(String(200), default="all-MiniLM-L6-v2")
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimensions: Mapped[int] = mapped_column(Integer, default=384)
    auto_embed_on_save: Mapped[bool] = mapped_column(Boolean, default=False)
    monthly_budget_usd: Mapped[float | None] = mapped_column(nullable=True)
    daily_cap_usd: Mapped[float | None] = mapped_column(nullable=True)
    last_full_embed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    project: Mapped["Project"] = relationship()  # type: ignore[name-defined]  # noqa: F821


class EntityEmbedding(Base):
    __tablename__ = "entity_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(255), default="main")
    entity_iri: Mapped[str] = mapped_column(String(2000))
    entity_type: Mapped[str] = mapped_column(String(50))
    label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    embedding_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50))
    model_name: Mapped[str] = mapped_column(String(200))
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "branch", "entity_iri", name="uq_entity_embedding"),
        CheckConstraint(
            "dimensions > 0 AND dimensions <= 16000 AND vector_dims(embedding) = dimensions",
            name="ck_entity_embeddings_dimensions",
        ),
        Index("ix_entity_embeddings_project_branch", "project_id", "branch"),
    )


class EntityEmbeddingStaging(Base):
    """A job-private snapshot that becomes visible only on atomic activation."""

    __tablename__ = "entity_embedding_staging"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("embedding_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    entity_iri: Mapped[str] = mapped_column(String(2000), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(255))
    entity_type: Mapped[str] = mapped_column(String(50))
    label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    embedding_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector() if Vector is not None else Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(50))
    model_name: Mapped[str] = mapped_column(String(200))
    deprecated: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        CheckConstraint(
            "dimensions > 0 AND dimensions <= 16000",
            name="ck_entity_embedding_staging_dimensions",
        ),
        CheckConstraint(
            "vector_dims(embedding) = dimensions",
            name="ck_entity_embedding_staging_vector_dimensions",
        ),
    )


class EmbeddingJob(Base):
    __tablename__ = "embedding_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    total_entities: Mapped[int] = mapped_column(Integer, default=0)
    embedded_entities: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship()  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        Index(
            "uq_embedding_job_active_project",
            "project_id",
            unique=True,
            postgresql_where=sql_text("status IN ('pending', 'running')"),
        ),
    )
