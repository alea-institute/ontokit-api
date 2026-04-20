"""Per-project lint configuration model."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from ontokit.models.project import Project

from ontokit.core.database import Base


class ProjectLintConfig(Base):
    """Per-project lint rule configuration.

    Stores which lint rules are enabled for a project, either via an explicit
    comma-separated list of rule IDs or via a preset lint level (1-5).
    When ``lint_level`` is set, it takes precedence over ``enabled_rules``.
    """

    __tablename__ = "project_lint_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        unique=True,
    )
    lint_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled_rules: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    project: Mapped["Project"] = relationship(back_populates="lint_config")

    def __repr__(self) -> str:
        return f"<ProjectLintConfig(project_id={self.project_id}, lint_level={self.lint_level})>"

    def get_enabled_rule_ids(self) -> set[str] | None:
        """Resolve the effective set of enabled rule IDs.

        Returns ``None`` when no configuration has been set (meaning all rules).
        """
        from ontokit.services.linter import ALL_RULE_IDS, get_rules_for_level

        if self.lint_level is not None:
            return get_rules_for_level(self.lint_level)
        if self.enabled_rules is not None:
            ids = {r.strip() for r in self.enabled_rules.split(",") if r.strip()}
            return ids & ALL_RULE_IDS
        return None
