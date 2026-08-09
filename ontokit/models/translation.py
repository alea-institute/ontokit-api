"""Durable provenance records for machine-translated ontology literals."""

from __future__ import annotations

import hashlib
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
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


__all__ = ["TRANSLATION_STATES", "TranslationRecord", "hash_literal_value"]
