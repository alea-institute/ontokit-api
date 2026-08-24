"""Commit-authoring identity for contributors (R14, R15).

Git history is permanent and, once mirrored, public. A contributor's real
email address must therefore never be written into it. This module resolves the
name/email pair used for every suggestion commit:

- By default: the contributor's display name plus a synthetic noreply alias
  (KTD9), which is stable per user, non-reversible, and distinct for two
  contributors who happen to share a display name.
- On opt-in with a VERIFIED address: that address, so the mirrored commit
  attributes natively to the contributor's GitHub account (R15).

There is deliberately no code path that falls back to the real address. An
absent, unverified, or not-opted-in preference resolves to the alias.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.config import settings
from ontokit.models.user_commit_identity import UserCommitIdentity

logger = logging.getLogger(__name__)

# Longest slug we allow in the local part, so the address stays readable and
# well within the RFC-5321 64-octet local-part limit alongside the hash suffix.
MAX_SLUG_LENGTH = 32

# Used when a display name slugs to nothing at all (e.g. a name written
# entirely in a script with no ASCII fold, or only emoji).
SLUG_FALLBACK = "contributor"

_NON_SLUG = re.compile(r"[^a-z0-9]+")


class _UnsetPreference:
    """Sentinel that distinguishes an omitted PATCH field from explicit null."""


_UNSET_PREFERENCE = _UnsetPreference()


def slugify_display_name(display_name: str | None) -> str:
    """Fold a display name to a safe email local-part fragment.

    NFKD-normalizes and drops combining marks so accented Latin names keep a
    recognizable slug, then collapses everything else to hyphens. Never returns
    an empty string — an empty local part would produce a malformed address.
    """
    if not display_name:
        return SLUG_FALLBACK
    folded = unicodedata.normalize("NFKD", display_name)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_SLUG.sub("-", ascii_only).strip("-")
    if not slug:
        return SLUG_FALLBACK
    return slug[:MAX_SLUG_LENGTH].strip("-") or SLUG_FALLBACK


def stable_user_suffix(user_id: str) -> str:
    """Eight hex characters that identify a user without revealing them.

    Salted with SECRET_KEY so the suffix is not a lookup key an outside
    observer can recompute from a guessed user ID.
    """
    digest = hashlib.sha256(f"{user_id}{settings.secret_key}".encode()).hexdigest()
    return digest[:8]


def noreply_alias(user_id: str, display_name: str | None) -> str:
    """The default commit author address for a contributor (KTD9)."""
    return f"{slugify_display_name(display_name)}-{stable_user_suffix(user_id)}@{_domain()}"


def anonymous_alias(session_id: str) -> str:
    """Commit author address for an anonymous suggestion session.

    Credited and stable within the session, but not linkable to a person and
    never the address the submitter typed into the credit modal.
    """
    return f"anonymous-{stable_user_suffix(session_id)}@{_domain()}"


def _domain() -> str:
    return settings.commit_noreply_domain or "users.noreply.ontokit.local"


class CommitIdentityService:
    """Resolves the (name, email) pair used to author suggestion commits."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_preference(self, user_id: str) -> UserCommitIdentity | None:
        result = await self.db.execute(
            select(UserCommitIdentity).where(UserCommitIdentity.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def resolve(
        self,
        user_id: str,
        display_name: str | None,
        *,
        is_anonymous: bool = False,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        """Return the (author_name, author_email) for a commit.

        Anonymous sessions never consult the preference table — they have no
        account to hold one, and the credit name the submitter supplied is used
        for the name only, never the address.
        """
        if is_anonymous:
            return (display_name or "Anonymous", anonymous_alias(session_id or user_id))

        name = display_name or "Contributor"
        preference = await self.get_preference(user_id)
        if (
            preference is not None
            and preference.use_verified_email
            and preference.commit_email_verified
            and preference.commit_email
        ):
            return (name, preference.commit_email)

        return (name, noreply_alias(user_id, display_name))

    async def set_preference(
        self,
        user_id: str,
        *,
        commit_email: str | None | _UnsetPreference = _UNSET_PREFERENCE,
        use_verified_email: bool | _UnsetPreference = _UNSET_PREFERENCE,
    ) -> UserCommitIdentity:
        """Create or update a contributor's authoring preference.

        Changing the address always resets ``commit_email_verified`` — an
        unverified address must never be honored, and re-pointing the
        preference is exactly the moment someone would try to.
        """
        preference = await self.get_preference(user_id)
        if preference is None:
            preference = UserCommitIdentity(user_id=user_id)
            self.db.add(preference)

        if not isinstance(commit_email, _UnsetPreference) and (
            commit_email != preference.commit_email
        ):
            preference.commit_email = commit_email or None
            preference.commit_email_verified = False
            if preference.commit_email is None:
                preference.use_verified_email = False
        if not isinstance(use_verified_email, _UnsetPreference):
            preference.use_verified_email = use_verified_email

        return preference


def get_commit_identity_service(db: AsyncSession) -> CommitIdentityService:
    """Factory function for dependency injection."""
    return CommitIdentityService(db)


__all__ = [
    "MAX_SLUG_LENGTH",
    "SLUG_FALLBACK",
    "CommitIdentityService",
    "anonymous_alias",
    "get_commit_identity_service",
    "noreply_alias",
    "slugify_display_name",
    "stable_user_suffix",
]
