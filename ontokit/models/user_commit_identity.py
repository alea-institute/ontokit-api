"""Per-user commit-authoring identity preferences (R14, R15).

By default a contributor's commits are authored with their display name and a
synthetic noreply alias (see ``ontokit.services.commit_identity``) so a real
email address never enters permanent, publicly mirrored git history.

A contributor may opt in to authoring with a *verified* address instead — e.g.
their GitHub noreply address — so mirrored commits attribute natively to their
GitHub account (R15). The address is only honored once ``commit_email_verified``
is true; an unverified address always falls back to the alias.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ontokit.core.database import Base


class UserCommitIdentity(Base):
    """Opt-in commit-authoring preferences for one user."""

    __tablename__ = "user_commit_identities"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)

    # Opt-in authoring address (R15). Never used unless verified.
    commit_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    commit_email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Master switch: even a verified address is only used when the contributor
    # has actively opted in.
    use_verified_email: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<UserCommitIdentity(user_id={self.user_id!r}, "
            f"verified={self.commit_email_verified}, opted_in={self.use_verified_email})>"
        )
