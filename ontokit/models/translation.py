"""Durable provenance records for machine-translated ontology literals."""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import Select

from ontokit.core.database import Base

if TYPE_CHECKING:
    from ontokit.models.project import Project, ProjectMember

TRANSLATION_STATES = ("verified", "provisional", "rejected")
_STATE_CHECK_SQL = "state IN ('verified', 'provisional', 'rejected')"


class ProjectTranslationConfig(Base):
    """Per-project translation generation and verification settings.

    A null primary provider uses the project's ``ProjectLLMConfig`` provider and key, but
    ``primary_model`` must be set because translation never falls back to
    ``ProjectLLMConfig.model``. A null verifier key continues to use the primary project LLM key.
    """

    __tablename__ = "project_translation_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    language_tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    verification_mechanism: Mapped[str] = mapped_column(
        String(20), default="consensus", nullable=False
    )
    consensus_threshold: Mapped[float] = mapped_column(Float, default=0.85, nullable=False)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.80, nullable=False)
    translate_definitions: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    translate_examples: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    speed_mode: Mapped[str] = mapped_column(String(20), default="batch", nullable=False)
    provisional_gate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    primary_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    primary_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    verifier_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    verifier_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Null deliberately means translation verification uses the primary project LLM key.
    verifier_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, onupdate=func.now()
    )

    project: Mapped[Project] = relationship()

    __table_args__ = (
        CheckConstraint(
            "verification_mechanism IN ('consensus', 'confidence')",
            name="ck_project_translation_verification_mechanism",
        ),
        CheckConstraint(
            "speed_mode IN ('batch', 'fast')", name="ck_project_translation_speed_mode"
        ),
        CheckConstraint(
            "consensus_threshold >= 0 AND consensus_threshold <= 1",
            name="ck_project_translation_consensus_threshold",
        ),
        CheckConstraint(
            "confidence_threshold >= 0 AND confidence_threshold <= 1",
            name="ck_project_translation_confidence_threshold",
        ),
    )


def hash_literal_value(value: str) -> str:
    """Hash a literal after NFC Unicode normalization and outer whitespace stripping."""
    normalized = unicodedata.normalize("NFC", value).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class TranslationRecord(Base):
    """Full operational provenance for one exact machine-translated literal."""

    __tablename__ = "translation_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    entity_iri: Mapped[str] = mapped_column(String(2000), nullable=False)
    predicate: Mapped[str] = mapped_column(String(2000), nullable=False)
    language: Mapped[str] = mapped_column(String(35), nullable=False)
    source_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    translated_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_version: Mapped[str] = mapped_column(String(200), nullable=False)
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="provisional", server_default="provisional"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirming_member_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_members.id", ondelete="SET NULL"), nullable=True
    )

    project: Mapped[Project] = relationship()
    confirming_member: Mapped[ProjectMember | None] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "entity_iri",
            "predicate",
            "language",
            "source_value_hash",
            "translated_value_hash",
            name="uq_translation_record_literal",
        ),
        CheckConstraint(_STATE_CHECK_SQL, name="ck_translation_record_state"),
        Index(
            "ix_translation_records_unconfirmed_era",
            "project_id",
            "created_at",
            postgresql_where=sql_text("confirmed_at IS NULL"),
        ),
    )

    @classmethod
    def unconfirmed_machine_records_before(
        cls, project_id: uuid.UUID, cutoff: datetime
    ) -> Select[tuple[TranslationRecord]]:
        """Select this project's unconfirmed machine records created before an era cutoff."""
        return (
            select(cls)
            .where(
                cls.project_id == project_id,
                cls.created_at < cutoff,
                cls.confirmed_at.is_(None),
                cls.state != "rejected",
            )
            .order_by(cls.created_at, cls.id)
        )

    def confirm(self, member_id: uuid.UUID, *, at: datetime | None = None) -> None:
        """Transition a provisional record to verified and stamp its confirmer."""
        if self.state != "provisional":
            raise ValueError("only provisional translation records can be confirmed")
        self.state = "verified"
        self.confirming_member_id = member_id
        self.confirmed_at = at or datetime.now(UTC)


class TranslationJob(Base):
    """Durable, resumable project backfill lifecycle."""

    __tablename__ = "translation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    era_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    never_confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    total_literals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_literals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship()

    __table_args__ = (
        Index(
            "uq_translation_job_active_project",
            "project_id",
            unique=True,
            postgresql_where=sql_text("status IN ('pending', 'running')"),
        ),
    )


class NativeReviewerLanguage(Base):
    """A project member's independently assignable native-reviewer language tag."""

    __tablename__ = "native_reviewer_languages"

    member_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("project_members.id", ondelete="CASCADE"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(35), primary_key=True)

    member: Mapped[ProjectMember] = relationship()


__all__ = [
    "ProjectTranslationConfig",
    "NativeReviewerLanguage",
    "TRANSLATION_STATES",
    "TranslationJob",
    "TranslationRecord",
    "hash_literal_value",
]
