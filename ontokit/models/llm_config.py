"""LLM configuration and audit log models for per-project LLM settings."""

__all__ = ["LLMAuditLog", "ProjectLLMConfig"]

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ontokit.core.database import Base


class ProjectLLMConfig(Base):
    """Per-project LLM provider configuration."""

    __tablename__ = "project_llm_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(50), default="openai")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # "quality" or "cheap" — sets the default tier used for LLM calls
    model_tier: Mapped[str] = mapped_column(String(20), default="quality")
    # Fernet-encrypted API key; null means no project key (users must BYO)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For local/custom providers (Ollama, LM Studio, llamafile, custom)
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Budget controls — None means unlimited / no cap
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_cap_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    project: Mapped["Project"] = relationship()  # type: ignore[name-defined]  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<ProjectLLMConfig(project_id={self.project_id}, provider={self.provider!r}, "
            f"model_tier={self.model_tier!r})>"
        )


class LLMAuditLog(Base):
    """Audit log entry for every LLM call made on behalf of a project.

    No prompt or response content is stored — metadata only (privacy-safe).
    """

    __tablename__ = "llm_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(String(255))
    model: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(50))
    # e.g. "llm/generate-suggestions", "llm/connection-test"
    endpoint: Mapped[str] = mapped_column(String(200))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_estimate_usd: Mapped[float] = mapped_column(Float)
    # True when the user supplied their own API key; these do NOT count against project budget
    is_byo_key: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    project: Mapped["Project"] = relationship()  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        Index("ix_llm_audit_project_date", "project_id", "created_at"),
        Index("ix_llm_audit_project_user", "project_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<LLMAuditLog(project_id={self.project_id}, user_id={self.user_id!r}, "
            f"model={self.model!r}, cost={self.cost_estimate_usd:.6f})>"
        )
