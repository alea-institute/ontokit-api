"""Contribution trust ladder service (R4-R11).

This module is the SINGLE place that answers trust questions. Every gate —
entity minting (R8), triage routing (R9), rate limiting (R10), auto-accept
eligibility (R11/R13) — reads ``resolve_tier`` and the helpers here. No route
re-derives a tier from roles (KTD3); a second derivation is a privilege-
escalation bug waiting to happen.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.core.auth import CurrentUser
from ontokit.models.project import Project, ProjectMember
from ontokit.models.suggestion_outcome import SuggestionOutcome, SuggestionOutcomeType
from ontokit.models.suggestion_session import SuggestionSession
from ontokit.schemas.trust import TrustOverride, TrustTier

logger = logging.getLogger(__name__)

# Existing project roles that outrank the ladder entirely (KTD4).
REVIEWER_ROLES = frozenset({"owner", "admin", "editor"})

# Prefix of the pseudo-user ID minted for anonymous suggestion sessions.
ANONYMOUS_USER_PREFIX = "anonymous-"

# Actor recorded on outcomes decided by the auto-accept sweep.
SYSTEM_AUTO_ACCEPT_ACTOR = "system:auto-accept"


def is_anonymous_user_id(user_id: str | None) -> bool:
    """True when the ID is an anonymous suggestion session's pseudo-user."""
    return bool(user_id) and str(user_id).startswith(ANONYMOUS_USER_PREFIX)


class TrustService:
    """Tier resolution, the append-only outcome log, and promotion."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- Tier resolution -------------------------------------------------

    @staticmethod
    def get_member(project: Project, user_id: str) -> ProjectMember | None:
        """Find a membership row from the project's already-loaded members.

        Reading from the loaded collection (rather than issuing a query) is what
        keeps the triage list free of an N+1.
        """
        for member in project.members:
            if member.user_id == user_id:
                return member
        return None

    @classmethod
    def resolve_tier(cls, project: Project, user: CurrentUser | None) -> TrustTier:
        """Resolve a caller's rung on the ladder.

        Implements the plan's tier-resolution flow exactly:
        unauthenticated / anonymous pseudo-user -> ``anonymous``; superadmin or
        owner/admin/editor -> ``reviewer`` (KTD4); otherwise the sticky
        ``trust_override`` (KTD1), then the materialized ``is_trusted`` flag.
        """
        if user is None or is_anonymous_user_id(user.id):
            return TrustTier.ANONYMOUS

        if getattr(user, "is_superadmin", False):
            return TrustTier.REVIEWER

        return cls.resolve_tier_for_member(user, cls.get_member(project, user.id))

    @classmethod
    def resolve_tier_for_member(
        cls,
        user: CurrentUser,
        member: ProjectMember | None,
    ) -> TrustTier:
        """Resolve a signed-in caller's tier from one membership row.

        This is the query-efficient counterpart to :meth:`resolve_tier`. It
        preserves the same precedence while allowing caller-scoped reads to
        avoid hydrating the project's complete member roster.
        """
        if is_anonymous_user_id(user.id):
            return TrustTier.ANONYMOUS

        if getattr(user, "is_superadmin", False):
            return TrustTier.REVIEWER

        if member is None:
            # Signed in with no membership row: the implicit-suggester rung.
            return TrustTier.UNTRUSTED

        if member.role in REVIEWER_ROLES:
            return TrustTier.REVIEWER

        override = member.trust_override or TrustOverride.NONE.value
        if override == TrustOverride.GRANTED.value:
            return TrustTier.TRUSTED
        if override in (TrustOverride.REFUSED.value, TrustOverride.REVOKED.value):
            # An admin decision is sticky and outranks the materialized flag.
            return TrustTier.UNTRUSTED

        return TrustTier.TRUSTED if member.is_trusted else TrustTier.UNTRUSTED

    async def load_project_and_member(
        self, project_id: UUID, user_id: str
    ) -> tuple[Project, ProjectMember | None] | None:
        """Load a project and only the requested caller's membership row."""
        result = await self.db.execute(
            select(Project, ProjectMember)
            .outerjoin(
                ProjectMember,
                and_(
                    ProjectMember.project_id == Project.id,
                    ProjectMember.user_id == user_id,
                ),
            )
            .where(Project.id == project_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        project, member = row
        return project, member

    @staticmethod
    def can_mint_entities(tier: TrustTier) -> bool:
        """R8 / KD4: minting new entities requires trusted status or above."""
        return tier in (TrustTier.TRUSTED, TrustTier.REVIEWER)

    @staticmethod
    def can_suggest(tier: TrustTier) -> bool:
        """Every rung may suggest — the ladder gates powers, not participation."""
        return tier in (
            TrustTier.ANONYMOUS,
            TrustTier.UNTRUSTED,
            TrustTier.TRUSTED,
            TrustTier.REVIEWER,
        )

    # --- Append-only outcome log ----------------------------------------

    async def record_outcome(
        self,
        project_id: UUID,
        session: SuggestionSession,
        outcome: SuggestionOutcomeType | str,
        decided_by: str | None,
        note: str | None = None,
        *,
        project: Project | None = None,
        decided_by_name: str | None = None,
    ) -> SuggestionOutcome:
        """Append one terminal outcome for a suggestion session (R5).

        The row is added to the CURRENT transaction and deliberately not
        committed here: the caller commits it together with the session's status
        change, so a merged suggestion can never exist without its outcome row.
        """
        is_anonymous = bool(getattr(session, "is_anonymous", False)) or is_anonymous_user_id(
            session.user_id
        )
        snapshot_captured_at = datetime.now(UTC)
        snapshot_tier: str | None = None
        snapshot_role: str | None = None

        if project is not None and not is_anonymous:
            try:
                submitter = CurrentUser(
                    id=session.user_id,
                    email=session.user_email,
                    name=session.user_name,
                )
                snapshot_tier = self.resolve_tier(project, submitter).value
                member = self.get_member(project, session.user_id)
                snapshot_role = member.role if member is not None else None
            except Exception as exc:  # noqa: BLE001 — audit capture must never block the outcome
                snapshot_tier = None
                snapshot_role = None
                # Metadata only: do not leak display attribution into logs.
                logger.warning(
                    "suggestion outcome snapshot resolution failed (%s): "
                    "project=%s session=%s user=%s",
                    type(exc).__name__,
                    project_id,
                    session.id,
                    session.user_id,
                )

        if is_anonymous:
            submitter_name = session.submitter_name
            submitter_email = session.submitter_email
        else:
            submitter_name = session.user_name
            submitter_email = session.user_email

        row = SuggestionOutcome(
            project_id=project_id,
            user_id=session.user_id,
            session_id=session.id,
            outcome=str(outcome),
            # R7: anonymous work is credited, never counted.
            counts_toward_promotion=not is_anonymous,
            is_anonymous=is_anonymous,
            decided_by=decided_by,
            note=note,
            snapshot_tier=snapshot_tier,
            snapshot_role=snapshot_role,
            submitter_name=submitter_name,
            submitter_email=submitter_email,
            decided_by_name=decided_by_name,
            snapshot_captured_at=snapshot_captured_at,
        )
        self.db.add(row)
        return row

    async def count_accepted(self, project_id: UUID, user_id: str) -> int:
        """Count promotion-eligible accepted outcomes for a contributor."""
        result = await self.db.execute(
            select(sa_func.count(SuggestionOutcome.id)).where(
                SuggestionOutcome.project_id == project_id,
                SuggestionOutcome.user_id == user_id,
                SuggestionOutcome.outcome == SuggestionOutcomeType.ACCEPTED.value,
                SuggestionOutcome.counts_toward_promotion.is_(True),
            )
        )
        return int(result.scalar() or 0)

    async def count_accepted_by_user(self, project_id: UUID, user_ids: list[str]) -> dict[str, int]:
        """Count promotion-eligible acceptances for a member roster in one query."""
        if not user_ids:
            return {}
        result = await self.db.execute(
            select(SuggestionOutcome.user_id, sa_func.count(SuggestionOutcome.id))
            .where(
                SuggestionOutcome.project_id == project_id,
                SuggestionOutcome.user_id.in_(user_ids),
                SuggestionOutcome.outcome == SuggestionOutcomeType.ACCEPTED.value,
                SuggestionOutcome.counts_toward_promotion.is_(True),
            )
            .group_by(SuggestionOutcome.user_id)
        )
        return {user_id: int(count) for user_id, count in result.all()}

    async def get_outcome_counts(self, project_id: UUID, user_id: str) -> tuple[int, int]:
        """Return accepted and total outcome counts with one aggregate query."""
        result = await self.db.execute(
            select(
                sa_func.count(SuggestionOutcome.id).filter(
                    SuggestionOutcome.outcome == SuggestionOutcomeType.ACCEPTED.value,
                    SuggestionOutcome.counts_toward_promotion.is_(True),
                ),
                sa_func.count(SuggestionOutcome.id),
            ).where(
                SuggestionOutcome.project_id == project_id,
                SuggestionOutcome.user_id == user_id,
            )
        )
        accepted, total = result.one()
        return int(accepted or 0), int(total or 0)

    async def count_outcomes(self, project_id: UUID, user_id: str) -> int:
        """Count ALL outcomes for a contributor (used by the first-suggestion check)."""
        result = await self.db.execute(
            select(sa_func.count(SuggestionOutcome.id)).where(
                SuggestionOutcome.project_id == project_id,
                SuggestionOutcome.user_id == user_id,
            )
        )
        return int(result.scalar() or 0)

    # --- Promotion --------------------------------------------------------

    async def evaluate_promotion(self, project: Project, user_id: str) -> bool:
        """Auto-promote a contributor who has crossed the threshold (R6).

        Returns True only on the transition to trusted, so the caller can send
        exactly one notification. An admin override of any kind blocks
        auto-promotion permanently (KTD1) — including ``granted``, which has
        already promoted them.
        """
        if is_anonymous_user_id(user_id):
            return False  # R7: promotion requires an account.

        member = self.get_member(project, user_id)
        if member is None:
            return False
        if member.is_trusted:
            return False
        if (member.trust_override or TrustOverride.NONE.value) != TrustOverride.NONE.value:
            return False

        threshold = project.trust_promotion_threshold or 5
        accepted = await self.count_accepted(project.id, user_id)
        if accepted < threshold:
            return False

        member.is_trusted = True
        member.trust_granted_at = datetime.now(UTC)
        member.trust_granted_by = "system:auto-promotion"
        logger.info(
            "trust auto-promotion: project=%s user=%s accepted=%s threshold=%s",
            project.id,
            user_id,
            accepted,
            threshold,
        )
        return True

    async def set_trust_override(
        self,
        project: Project,
        target_user_id: str,
        override: TrustOverride | str,
        actor: CurrentUser,
    ) -> ProjectMember:
        """Admin grant / refuse / revoke / clear (R6).

        Clearing back to ``none`` hands the member back to the ladder and
        immediately re-evaluates auto-promotion, so an admin who reverses a
        refusal does not have to wait for the contributor's next acceptance.
        """
        member = self.get_member(project, target_user_id)
        if member is None:
            raise ValueError("Member not found in this project")

        value = str(override)
        member.trust_override = value

        if value == TrustOverride.GRANTED.value:
            member.is_trusted = True
            member.trust_granted_at = datetime.now(UTC)
            member.trust_granted_by = actor.id
        elif value in (TrustOverride.REFUSED.value, TrustOverride.REVOKED.value):
            member.is_trusted = False
            member.trust_granted_at = None
            member.trust_granted_by = None
        else:  # none — back under the ladder's control
            # A prior explicit grant materializes ``is_trusted=True``. Clear
            # that state before re-evaluating, otherwise evaluate_promotion's
            # early return preserves the grant forever even below threshold.
            member.is_trusted = False
            member.trust_granted_at = None
            member.trust_granted_by = None
            await self.evaluate_promotion(project, target_user_id)

        # Privilege changes must leave a trace. Metadata only, mirroring the
        # posture of the LLM member-flags audit line.
        logger.info(
            "trust override set: project=%s actor=%s target=%s override=%s is_trusted=%s",
            project.id,
            actor.id,
            target_user_id,
            value,
            member.is_trusted,
        )
        return member

    # --- Auto-accept eligibility -----------------------------------------

    @staticmethod
    def is_auto_accept_eligible(
        project: Project, session: SuggestionSession, tier: TrustTier
    ) -> bool:
        """Whether a submitted session may ever auto-merge (R11, R13; KD5).

        The single predicate every scheduling site must go through, so R13's
        "LLM output is never auto-accepted, at any tier, ever" cannot be
        bypassed by a new call site.
        """
        if tier is not TrustTier.TRUSTED:
            return False
        if not project.auto_accept_enabled:
            return False
        if getattr(session, "is_anonymous", False):
            return False
        # R13 is the last word: LLM output never auto-accepts, at any tier.
        return not getattr(session, "is_llm_generated", False)


def get_trust_service(db: AsyncSession) -> TrustService:
    """Factory function for dependency injection."""
    return TrustService(db)
