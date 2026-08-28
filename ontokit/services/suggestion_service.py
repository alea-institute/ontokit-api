"""Suggestion session service for managing suggester workflows."""

import json
import logging
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ontokit.core.anonymous_token import create_anonymous_token
from ontokit.core.auth import ANONYMOUS_USER, CurrentUser
from ontokit.core.beacon_token import create_beacon_token, verify_beacon_token
from ontokit.core.limits import (
    MAX_ANONYMOUS_SESSION_BYTES,
    MAX_ANONYMOUS_SESSION_COMMITS,
)
from ontokit.git import GitRepositoryService, get_git_service
from ontokit.models.embedding import EmbeddingJob
from ontokit.models.project import Project, ProjectMember
from ontokit.models.pull_request import PRStatus, PullRequest
from ontokit.models.suggestion_outcome import SuggestionOutcomeType
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.anonymous_suggestion import (
    AnonymousSessionCreateResponse,
    AnonymousSubmitRequest,
    AnonymousSubmitResponse,
)
from ontokit.schemas.pull_request import PRCreate
from ontokit.schemas.suggestion import (
    BulkReviewAction,
    BulkReviewFailure,
    BulkReviewRequest,
    BulkReviewResponse,
    SuggestionBeaconRequest,
    SuggestionCapabilitiesResponse,
    SuggestionQueue,
    SuggestionRejectRequest,
    SuggestionRequestChangesRequest,
    SuggestionResubmitRequest,
    SuggestionSaveRequest,
    SuggestionSaveResponse,
    SuggestionSessionListResponse,
    SuggestionSessionResponse,
    SuggestionSessionSummary,
    SuggestionSubmitRequest,
    SuggestionSubmitResponse,
    SuggestionUser,
)
from ontokit.schemas.trust import TrustTier
from ontokit.services.branch_lock import branch_write_lock, pull_request_write_locks
from ontokit.services.commit_identity import CommitIdentityService
from ontokit.services.notification_service import NotificationService
from ontokit.services.pull_request_service import PullRequestService, get_pull_request_service
from ontokit.services.rdf_utils import get_entity_type
from ontokit.services.trust_rate_limiter import (
    TrustLimiterRedis,
    TrustLimitStatus,
    check_and_consume,
)
from ontokit.services.trust_service import (
    SYSTEM_AUTO_ACCEPT_ACTOR,
    TrustService,
    is_anonymous_user_id,
)
from ontokit.services.verification import get_verification_provider

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ExternalPRFinalization:
    """Complete context for synchronizing one claimed PR outside its locks."""

    service: PullRequestService
    db_pr: PullRequest
    project: Project
    create: PRCreate


@dataclass(frozen=True, slots=True)
class _PendingSuggestionPullRequest:
    """A committed PR claim whose external fan-out must run after lock release."""

    id: UUID
    pr_number: int
    title: str
    github_pr_url: str | None
    external_finalization: _ExternalPRFinalization | None = None


AUTO_ACCEPT_BATCH_SIZE = 100
AUTO_ACCEPT_LEASE = timedelta(minutes=15)
MAX_NEW_ENTITIES_PER_SUBMISSION = 25

_ENTITY_DECLARATION_TYPES = frozenset(
    {
        RDFS.Class,
        OWL.Class,
        RDF.Property,
        OWL.ObjectProperty,
        OWL.DatatypeProperty,
        OWL.AnnotationProperty,
        OWL.FunctionalProperty,
        OWL.InverseFunctionalProperty,
        OWL.TransitiveProperty,
        OWL.SymmetricProperty,
        OWL.AsymmetricProperty,
        OWL.ReflexiveProperty,
        OWL.IrreflexiveProperty,
    }
)


class SuggestionService:
    """Service for suggestion session CRUD, save, submit, and auto-submit."""

    def __init__(
        self,
        db: AsyncSession,
        git_service: GitRepositoryService | None = None,
    ) -> None:
        self.db = db
        self.git_service = git_service or get_git_service()
        self.trust = TrustService(db)
        self.commit_identity = CommitIdentityService(db)

    # --- Helpers ---

    @staticmethod
    def _check_anonymous_write_budget(
        session: SuggestionSession,
        content: bytes,
    ) -> int:
        """Return the UTF-8 write size or refuse an exhausted session budget."""
        if session.changes_count >= MAX_ANONYMOUS_SESSION_COMMITS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Anonymous session commit limit reached",
            )

        content_bytes = len(content)
        recorded_bytes = session.anonymous_content_bytes or 0
        if recorded_bytes + content_bytes > MAX_ANONYMOUS_SESSION_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Anonymous session content limit reached",
            )
        return content_bytes

    async def _get_project(self, project_id: UUID) -> Project:
        """Get a project by ID with members loaded, or raise 404."""
        result = await self.db.execute(
            select(Project).options(selectinload(Project.members)).where(Project.id == project_id)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        return project

    async def _get_project_record(self, project_id: UUID) -> Project:
        """Get a project without hydrating its member roster."""
        result = await self.db.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project not found",
            )
        return project

    def _get_user_role(self, project: Project, user: CurrentUser) -> str | None:
        """Get user's role in the project."""
        for member in project.members:
            if member.user_id == user.id:
                return member.role
        return None

    def _can_suggest(self, role: str | None, user: CurrentUser) -> bool:
        """Check if the user's role allows suggesting."""
        if user.is_superadmin:
            return True
        return role in ("owner", "admin", "editor", "suggester")

    async def _verify_project_access(self, project_id: UUID, user: CurrentUser) -> Project:
        """Verify the user still has suggest permissions, and return the project.

        Returning the loaded project lets callers resolve the trust tier from
        its members collection without a second fetch.
        """
        project = await self._get_project(project_id)
        role = self._get_user_role(project, user)
        if not self._can_suggest(role, user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You no longer have permission to suggest changes",
            )
        return project

    def _get_git_ontology_path(self, project: Project) -> str:
        """Get the ontology file path within the git repo."""
        if project.source_file_path:
            path = os.path.normpath(project.source_file_path).lstrip("/\\")
            if path.startswith(".."):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid ontology path",
                )
            return path
        return "ontology.ttl"

    @staticmethod
    def _declared_entity_iris(content: bytes | str) -> set[str]:
        """Return explicitly declared RDF class/property IRIs in Turtle content."""
        graph = Graph()
        try:
            graph.parse(data=content, format="turtle")
        except Exception as e:  # rdflib exposes parser-specific exception subclasses
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid Turtle content",
            ) from e
        return {
            str(subject)
            for subject, object_type in graph.subject_objects(RDF.type)
            if isinstance(subject, URIRef) and object_type in _ENTITY_DECLARATION_TYPES
        }

    @staticmethod
    def _declared_entities(graph: Graph) -> set[URIRef]:
        """Return explicitly declared RDF class/property subjects from a graph."""
        return {
            subject
            for subject, object_type in graph.subject_objects(RDF.type)
            if isinstance(subject, URIRef) and object_type in _ENTITY_DECLARATION_TYPES
        }

    async def _validate_submission_content(
        self,
        project_id: UUID,
        branch: str,
        filename: str,
        content: str,
        billing_user_id: str | None,
    ) -> None:
        """Re-run duplicate gates against current fingerprint-bound decisions."""
        proposed = Graph()
        try:
            proposed.parse(data=content, format="turtle")
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Suggestion content is not valid Turtle",
            ) from exc

        baseline = Graph()
        default_branch = self.git_service.get_default_branch(project_id)
        try:
            baseline_content = self.git_service.get_file_from_branch(
                project_id, default_branch, filename
            )
            baseline.parse(data=baseline_content.decode("utf-8"), format="turtle")
        except KeyError:
            pass

        new_entities = self._declared_entities(proposed) - self._declared_entities(baseline)
        if len(new_entities) > MAX_NEW_ENTITIES_PER_SUBMISSION:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "message": "Suggestion adds too many entities for duplicate validation",
                    "code": "SUGGESTION_ENTITY_LIMIT",
                    "new_entity_count": len(new_entities),
                    "max_new_entities": MAX_NEW_ENTITIES_PER_SUBMISSION,
                },
            )
        if not new_entities:
            return
        if billing_user_id is None or billing_user_id == ANONYMOUS_USER.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="An authenticated identity is required to validate new entities",
            )

        baseline_entities = self._declared_entities(baseline)
        baseline_labels = {
            str(label).strip().casefold(): entity
            for entity in baseline_entities
            for label in baseline.objects(entity, RDFS.label)
            if isinstance(label, Literal) and str(label).strip()
        }

        from ontokit.services.duplicate_check_service import DuplicateCheckService

        duplicate_service = DuplicateCheckService(self.db)
        excluded_iris = {str(iri) for iri in new_entities}
        for entity in new_entities:
            labels = [
                label
                for label in proposed.objects(entity, RDFS.label)
                if isinstance(label, Literal)
            ]
            parents = [
                str(parent)
                for parent in proposed.objects(entity, RDFS.subClassOf)
                if isinstance(parent, URIRef)
            ]
            checked_labels: set[str] = set()
            for index, label in enumerate(labels):
                normalized_label = str(label).strip().casefold()
                existing = baseline_labels.get(normalized_label)
                if index != 0 and existing is None:
                    continue
                if normalized_label in checked_labels:
                    continue
                checked_labels.add(normalized_label)
                duplicate = await duplicate_service.check(
                    project_id,
                    str(label),
                    entity_type=get_entity_type(proposed, entity),
                    parent_iri=parents[0] if parents else None,
                    billing_user_id=billing_user_id,
                    exclude_branch=branch,
                    exclude_iris=excluded_iris,
                    proposed_iri=str(entity),
                )
                if existing is not None and existing != entity:
                    pair = {str(entity), str(existing)}
                    pair_is_distinct = any(
                        {decision.iri_a, decision.iri_b} == pair
                        for decision in duplicate.suppressed_decisions
                    )
                    if not pair_is_distinct:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Suggestion duplicates an existing entity label",
                        )
                if duplicate.verdict == "block":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Suggestion is a semantic duplicate of an existing entity",
                    )

    def _assert_branch_content_can_mint(
        self,
        project: Project,
        user: CurrentUser | None,
        current_content: bytes | str,
        proposed_content: bytes | str,
    ) -> None:
        """Enforce minting from the actual branch delta, never a client hint."""
        tier = self.trust.resolve_tier(project, user)
        if self.trust.can_mint_entities(tier):
            return
        current_entities = self._declared_entity_iris(current_content)
        proposed_entities = self._declared_entity_iris(proposed_content)
        if proposed_entities - current_entities:
            self._assert_can_mint(project, user)

    async def _enqueue_branch_refresh(
        self,
        project_id: UUID,
        branch: str,
        *,
        entity_iri: str | None = None,
        full_embedding: bool = False,
    ) -> None:
        """Keep duplicate and ontology indexes current after suggestion writes."""
        try:
            from ontokit.api.utils.redis import get_arq_pool

            pool = await get_arq_pool()
            await pool.enqueue_job("run_ontology_index_task", str(project_id), branch, None)
            if entity_iri:
                await pool.enqueue_job(
                    "run_single_entity_embed_task", str(project_id), branch, entity_iri
                )
            elif full_embedding:
                job = EmbeddingJob(project_id=project_id, branch=branch, status="pending")
                self.db.add(job)
                try:
                    await self.db.commit()
                except IntegrityError:
                    await self.db.rollback()
                    logger.info(
                        "Skipping duplicate active embedding refresh for project=%s branch=%s",
                        project_id,
                        branch,
                    )
                    return
                try:
                    await pool.enqueue_job(
                        "run_embedding_generation_task", str(project_id), branch, str(job.id)
                    )
                except Exception as exc:
                    job.status = "failed"
                    job.error_message = f"Failed to enqueue embedding job: {exc}"
                    job.completed_at = datetime.now(UTC)
                    await self.db.commit()
                    raise
        except Exception:
            logger.warning(
                "Failed to enqueue suggestion index refresh for project=%s branch=%s",
                project_id,
                branch,
                exc_info=True,
            )

    async def _get_session(self, project_id: UUID, session_id: str) -> SuggestionSession:
        """Get a suggestion session or raise 404."""
        result = await self.db.execute(
            select(SuggestionSession).where(
                SuggestionSession.project_id == project_id,
                SuggestionSession.session_id == session_id,
            )
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Suggestion session not found",
            )
        return session

    def _verify_ownership(self, session: SuggestionSession, user: CurrentUser) -> None:
        """Verify the user owns the session."""
        if session.user_id != user.id and not user.is_superadmin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not own this suggestion session",
            )

    def _parse_entities_modified(self, session: SuggestionSession) -> list[str]:
        """Parse the JSON entities_modified field into a list."""
        if not session.entities_modified:
            return []
        try:
            return json.loads(session.entities_modified)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, TypeError):
            return []

    def _update_entities_modified(self, session: SuggestionSession, label: str) -> None:
        """Add a label to the entities_modified list (deduplicated)."""
        entities = self._parse_entities_modified(session)
        if label not in entities:
            entities.append(label)
        session.entities_modified = json.dumps(entities)

    # --- Public methods ---

    async def create_session(
        self, project_id: UUID, user: CurrentUser
    ) -> SuggestionSessionResponse:
        """Create a new suggestion session with a dedicated branch."""
        project = await self._get_project(project_id)
        role = self._get_user_role(project, user)

        if not self._can_suggest(role, user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to suggest changes",
            )

        # Check for existing active session
        result = await self.db.execute(
            select(SuggestionSession).where(
                SuggestionSession.project_id == project_id,
                SuggestionSession.user_id == user.id,
                SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            return SuggestionSessionResponse(
                session_id=existing.session_id,
                branch=existing.branch,
                created_at=existing.created_at,
                beacon_token=existing.beacon_token,
            )

        # Generate identifiers
        session_id = f"s_{secrets.token_hex(8)}"
        user_prefix = user.id[:8]
        branch = f"suggest/{user_prefix}/{session_id}"
        beacon_token = create_beacon_token(session_id)

        # Create the git branch
        try:
            self.git_service.create_branch(project_id, branch)
        except Exception as e:
            logger.error(f"Failed to create suggestion branch: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create suggestion branch",
            ) from e

        # Create the database record
        db_session = SuggestionSession(
            project_id=project_id,
            user_id=user.id,
            user_name=user.name,
            user_email=user.email,
            session_id=session_id,
            branch=branch,
            beacon_token=beacon_token,
        )
        try:
            self.db.add(db_session)
            await self.db.commit()
        except IntegrityError:
            # Race: another request created an active session concurrently
            await self.db.rollback()
            try:
                self.git_service.delete_branch(project_id, branch, force=True)
            except Exception:
                logger.warning(f"Failed to clean up orphaned branch {branch}")
            # Return the existing session
            result2 = await self.db.execute(
                select(SuggestionSession).where(
                    SuggestionSession.project_id == project_id,
                    SuggestionSession.user_id == user.id,
                    SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
                )
            )
            existing = result2.scalar_one_or_none()
            if existing:
                return SuggestionSessionResponse(
                    session_id=existing.session_id,
                    branch=existing.branch,
                    created_at=existing.created_at,
                    beacon_token=existing.beacon_token,
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create suggestion session",
            ) from None
        except Exception:
            await self.db.rollback()
            try:
                self.git_service.delete_branch(project_id, branch, force=True)
            except Exception:
                logger.warning(f"Failed to clean up orphaned branch {branch}")
            raise

        # Refresh outside the branch-cleanup try/except so a refresh failure
        # after a successful commit does not trigger branch deletion.
        try:
            await self.db.refresh(db_session)
        except Exception:
            logger.warning("Suggestion session %s committed but refresh failed", session_id)
            # Re-fetch from DB since the ORM instance may be stale
            re_result = await self.db.execute(
                select(SuggestionSession).where(
                    SuggestionSession.project_id == project_id,
                    SuggestionSession.session_id == session_id,
                )
            )
            db_session = re_result.scalar_one()

        return SuggestionSessionResponse(
            session_id=db_session.session_id,
            branch=db_session.branch,
            created_at=db_session.created_at,
            beacon_token=db_session.beacon_token,
        )

    async def save(
        self,
        project_id: UUID,
        session_id: str,
        data: SuggestionSaveRequest,
        user: CurrentUser,
    ) -> SuggestionSaveResponse:
        """Save content to the suggestion branch."""
        session = await self._get_session(project_id, session_id)
        self._verify_ownership(session, user)
        await self._verify_project_access(project_id, user)

        if session.status != SuggestionSessionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot save",
            )

        project = await self._get_project(project_id)
        filename = self._get_git_ontology_path(project)

        # Serialize git writes per branch to prevent lost commits
        # R14: never author with the contributor's real email address.
        author_name, author_email = await self.commit_identity.resolve(
            session.user_id, session.user_name
        )

        async with branch_write_lock(self.db, project_id, session.branch):
            await self.db.refresh(session)
            if session.status != SuggestionSessionStatus.ACTIVE.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Session is {session.status}, cannot save",
                )
            current_content = self.git_service.get_file_from_branch(
                project_id, session.branch, filename
            )
            self._assert_branch_content_can_mint(project, user, current_content, data.content)
            # Commit to the suggestion branch
            commit_message = f"Update {data.entity_label}"
            try:
                commit_info = self.git_service.commit_to_branch(  # type: ignore[attr-defined]
                    project_id=project_id,
                    branch_name=session.branch,
                    ontology_content=data.content.encode("utf-8"),
                    filename=filename,
                    message=commit_message,
                    author_name=author_name,
                    author_email=author_email,
                )
            except Exception as e:
                logger.error(f"Failed to save suggestion: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to save suggestion to branch",
                ) from e

            # Update session metadata
            session.changes_count += 1
            self._update_entities_modified(session, data.entity_label)
            session.last_activity = datetime.now(UTC)
            try:
                await self.db.commit()
            except Exception as e:
                await self.db.rollback()
                logger.error(
                    "Failed to update session metadata after successful git commit: "
                    "session=%s branch=%s commit=%s error=%s",
                    session.session_id,
                    session.branch,
                    commit_info.hash,
                    e,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Saved to branch but failed to update session metadata",
                ) from e

        return SuggestionSaveResponse(
            commit_hash=commit_info.hash,
            branch=session.branch,
            changes_count=session.changes_count,
        )

    async def submit(
        self,
        project_id: UUID,
        session_id: str,
        data: SuggestionSubmitRequest,
        user: CurrentUser,
        *,
        verification_token: str | None = None,
        client_ip: str | None = None,
        redis: TrustLimiterRedis | None = None,
    ) -> SuggestionSubmitResponse:
        """Submit the suggestion session by creating a PR."""
        session = await self._get_session(project_id, session_id)
        self._verify_ownership(session, user)
        project = await self._verify_project_access(project_id, user)

        if session.status != SuggestionSessionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot submit",
            )

        if session.changes_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No changes to submit",
            )

        # Human verification may call a remote provider, so keep it outside the
        # PR allocation critical section. Consume the daily allowance only after
        # the refreshed session and submitted content are both eligible.
        await self._verify_untrusted_human(project, session, user, verification_token, client_ip)
        verification_passed = session.verification_passed

        filename = self._get_git_ontology_path(project)
        default_branch = self.git_service.get_default_branch(project_id)
        async with pull_request_write_locks(self.db, project_id, {session.branch, default_branch}):
            await self.db.refresh(session)
            if verification_passed:
                session.verification_passed = True
            if session.status != SuggestionSessionStatus.ACTIVE.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Session is {session.status}, cannot submit",
                )
            content = self.git_service.get_file_from_branch(project_id, session.branch, filename)
            await self._validate_submission_content(
                project_id,
                session.branch,
                filename,
                content.decode("utf-8"),
                str(user.id),
            )
            await self._consume_untrusted_submission(project, user, redis)

            claimed_pr = await self._create_pr_for_session_already_locked(
                project_id,
                session,
                user,
                data.summary,
                "submitted",
                default_branch,
            )
        return await self._finalize_pr_for_session(
            project_id, user, data.summary, "submitted", claimed_pr
        )

    async def _verify_untrusted_human(
        self,
        project: Project,
        session: SuggestionSession,
        user: CurrentUser,
        verification_token: str | None,
        client_ip: str | None,
    ) -> None:
        """Complete first-submission verification for the untrusted rung (R10).

        Only the untrusted rung is gated: trusted contributors have earned their
        way past it, and reviewers were never subject to it. Anonymous sessions
        keep their separate, DB-backed per-IP session limit.
        """
        if self.trust.resolve_tier(project, user) is not TrustTier.UNTRUSTED:
            return

        # First suggestion on this project: challenge once (F2). Persisted on
        # the session so a retry after a network blip does not re-challenge.
        if not session.verification_passed:
            provider = get_verification_provider()
            is_first_suggestion = (
                provider.enabled and await self.trust.count_outcomes(project.id, user.id) == 0
            )
            if is_first_suggestion and not await provider.verify(verification_token, client_ip):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "reason": "verification_required",
                        "message": "Please complete the verification challenge to continue.",
                    },
                )
            session.verification_passed = True

    async def _consume_untrusted_submission(
        self,
        project: Project,
        user: CurrentUser,
        redis: TrustLimiterRedis | None,
    ) -> None:
        """Consume allowance only for an eligible untrusted submission."""
        if self.trust.resolve_tier(project, user) is not TrustTier.UNTRUSTED:
            return

        project_id = project.id
        decision = await check_and_consume(redis, str(project_id), user.id)
        if decision.status is TrustLimitStatus.UNAVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "reason": "submission_limiter_unavailable",
                    "message": "Suggestion submissions are temporarily unavailable. Try again.",
                },
            )
        if decision.status is TrustLimitStatus.EXHAUSTED:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "reason": "daily_limit_reached",
                    "message": (
                        "You have reached today's suggestion limit. It resets at midnight UTC."
                    ),
                },
            )
        logger.debug(
            "Untrusted submission allowed: project=%s user=%s remaining=%s",
            project_id,
            user.id,
            decision.remaining,
        )

    async def _create_pr_for_session(
        self,
        project_id: UUID,
        session: SuggestionSession,
        user: CurrentUser,
        summary: str | None,
        new_status: str,
    ) -> SuggestionSubmitResponse:
        """Create a suggestion PR while acquiring its lock set exactly once."""
        default_branch = self.git_service.get_default_branch(project_id)
        async with pull_request_write_locks(self.db, project_id, {session.branch, default_branch}):
            claimed_pr = await self._create_pr_for_session_already_locked(
                project_id,
                session,
                user,
                summary,
                new_status,
                default_branch,
            )
        return await self._finalize_pr_for_session(
            project_id, user, summary, new_status, claimed_pr
        )

    async def _create_pr_for_session_already_locked(
        self,
        project_id: UUID,
        session: SuggestionSession,
        user: CurrentUser,
        summary: str | None,
        new_status: str,
        default_branch: str,
    ) -> SuggestionSubmitResponse | _PendingSuggestionPullRequest:
        """Claim and bind a suggestion PR while the caller holds all PR locks."""
        entities = self._parse_entities_modified(session)
        entity_list = ", ".join(entities[:5])
        if len(entities) > 5:
            entity_list += f" (+{len(entities) - 5} more)"

        title = f"Suggestion: Update {entity_list}" if entities else "Suggestion"
        if len(title) > 500:
            title = title[:497] + "..."

        body_parts = []
        if summary:
            body_parts.append(summary)
        body_parts.append(f"\n**Entities modified** ({session.changes_count} changes):")
        for entity in entities:
            body_parts.append(f"- {entity}")
        is_anonymous = getattr(session, "is_anonymous", False)
        if is_anonymous:
            submitter_name = getattr(session, "submitter_name", None)
            if submitter_name:
                body_parts.append(f"\n*Submitted by {submitter_name}*")
            else:
                body_parts.append("\n*Submitted anonymously*")
        else:
            body_parts.append(f"\n*Submitted by {session.user_name or session.user_id}*")
        description = "\n".join(body_parts)

        # Check for an existing PR on this branch (idempotency on retry)
        existing_pr_result = await self.db.execute(
            select(PullRequest).where(
                PullRequest.project_id == project_id,
                PullRequest.source_branch == session.branch,
                PullRequest.status == PRStatus.OPEN.value,
            )
        )
        existing_pr = existing_pr_result.scalar_one_or_none()
        if existing_pr:
            # PR already created (previous attempt failed after PR but before session update)
            await self._bind_session_to_pr(
                project_id, session, user, new_status, existing_pr.id, existing_pr.pr_number
            )
            return SuggestionSubmitResponse(
                pr_number=existing_pr.pr_number,
                pr_url=existing_pr.github_pr_url,
                status=new_status,
            )

        if new_status != SuggestionSessionStatus.SUBMITTED.value or is_anonymous:
            project = await self._get_project(project_id)
            filename = self._get_git_ontology_path(project)
            content = self.git_service.get_file_from_branch(project_id, session.branch, filename)
            await self._validate_submission_content(
                project_id,
                session.branch,
                filename,
                content.decode("utf-8"),
                None if is_anonymous else str(user.id),
            )

        # Create PR via the existing PR service
        pr_service = get_pull_request_service(self.db)
        pr_create = PRCreate(
            title=title,
            description=description,
            source_branch=session.branch,
            target_branch=default_branch,
        )

        pr_project: Project | None = None
        db_pr: PullRequest
        try:
            db_pr, pr_project = await pr_service._claim_pull_request_already_locked(
                project_id, pr_create, user
            )
        except HTTPException as e:
            # If the user doesn't have editor role for PR creation,
            # fall back to creating the PR directly
            if e.status_code == status.HTTP_403_FORBIDDEN:
                db_pr = await self._create_pr_directly(project_id, pr_create, session)
            elif e.status_code == status.HTTP_409_CONFLICT:
                raced_pr_result = await self.db.execute(
                    select(PullRequest).where(
                        PullRequest.project_id == project_id,
                        PullRequest.source_branch == session.branch,
                        PullRequest.status == PRStatus.OPEN.value,
                    )
                )
                raced_pr = raced_pr_result.scalar_one_or_none()
                if raced_pr is None:
                    raise
                await self._bind_session_to_pr(
                    project_id, session, user, new_status, raced_pr.id, raced_pr.pr_number
                )
                return SuggestionSubmitResponse(
                    pr_number=raced_pr.pr_number,
                    pr_url=raced_pr.github_pr_url,
                    status=new_status,
                )
            else:
                raise

        # Snapshot ORM fields before the session commit can expire them, then
        # make the session-to-PR link durable while locks are still held.
        pending = _PendingSuggestionPullRequest(
            id=db_pr.id,
            pr_number=db_pr.pr_number,
            title=db_pr.title,
            github_pr_url=db_pr.github_pr_url,
            external_finalization=(
                _ExternalPRFinalization(pr_service, db_pr, pr_project, pr_create)
                if pr_project is not None
                else None
            ),
        )
        await self._bind_session_to_pr(
            project_id, session, user, new_status, pending.id, pending.pr_number
        )
        return pending

    async def _bind_session_to_pr(
        self,
        project_id: UUID,
        session: SuggestionSession,
        user: CurrentUser,
        new_status: str,
        pr_id: UUID,
        pr_number: int,
    ) -> None:
        """Persist the common idempotent session-to-PR state transition."""
        session.status = new_status
        session.pr_number = pr_number
        session.pr_id = pr_id
        session.last_activity = datetime.now(UTC)
        await self._schedule_auto_accept(project_id, session, user)
        await self.db.commit()

    async def _finalize_pr_for_session(
        self,
        project_id: UUID,
        user: CurrentUser,
        summary: str | None,
        new_status: str,
        claimed_pr: SuggestionSubmitResponse | _PendingSuggestionPullRequest,
    ) -> SuggestionSubmitResponse:
        """Run GitHub synchronization and notifications after lock release."""
        if isinstance(claimed_pr, SuggestionSubmitResponse):
            return claimed_pr

        pr_title = claimed_pr.title
        pr_url = claimed_pr.github_pr_url
        if (finalization := claimed_pr.external_finalization) is not None:
            pr_response = await finalization.service._finalize_created_pull_request(
                project_id,
                finalization.create,
                user,
                finalization.project,
                finalization.db_pr,
            )
            pr_title = pr_response.title
            pr_url = pr_response.github_pr_url

        # Notify project editors/admins about the suggestion
        project = await self._get_project(project_id)
        notification_type = (
            "suggestion_auto_submitted"
            if new_status == SuggestionSessionStatus.AUTO_SUBMITTED.value
            else "suggestion_submitted"
        )
        notif = NotificationService(self.db)
        await notif.notify_project_roles(
            project_id=project_id,
            project_name=project.name,
            roles=["owner", "admin", "editor"],
            notification_type=notification_type,
            title=f"Suggestion submitted: {pr_title[:80]}",
            body=summary[:200] if summary else None,
            target_id=str(claimed_pr.id),
            exclude_user_id=user.id,
        )

        await self.db.commit()

        return SuggestionSubmitResponse(
            pr_number=claimed_pr.pr_number,
            pr_url=pr_url,
            status=new_status,
        )

    async def _create_pr_directly(
        self,
        project_id: UUID,
        pr_create: PRCreate,
        session: SuggestionSession,
    ) -> "PullRequest":
        """Create a PR record directly while the caller holds all PR locks."""
        from sqlalchemy import func as sa_func

        author_id = session.user_id
        author_name = session.user_name
        author_email = session.user_email

        max_retries = 3
        for attempt in range(max_retries):
            existing_result = await self.db.execute(
                select(PullRequest).where(
                    PullRequest.project_id == project_id,
                    PullRequest.source_branch == pr_create.source_branch,
                    PullRequest.status == PRStatus.OPEN.value,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing is not None:
                return existing

            max_number_result = await self.db.execute(
                select(sa_func.max(PullRequest.pr_number)).where(
                    PullRequest.project_id == project_id
                )
            )
            max_number = max_number_result.scalar() or 0
            pr_number = max_number + 1

            db_pr = PullRequest(
                project_id=project_id,
                pr_number=pr_number,
                title=pr_create.title,
                description=pr_create.description,
                source_branch=pr_create.source_branch,
                target_branch=pr_create.target_branch,
                author_id=author_id,
                author_name=author_name,
                author_email=author_email,
                status=PRStatus.OPEN.value,
            )
            try:
                async with self.db.begin_nested():
                    self.db.add(db_pr)
                    await self.db.flush()
            except IntegrityError:
                if attempt == max_retries - 1:
                    raise
                continue
            await self.db.refresh(db_pr)
            return db_pr

        # Unreachable, but satisfies type checker
        raise RuntimeError("Failed to allocate PR number")

    def _summary_tier(
        self,
        project: Project | None,
        s: SuggestionSession,
        members_by_user_id: Mapping[str, ProjectMember] | None = None,
    ) -> TrustTier | None:
        """Resolve the submitter's tier for a review-queue row.

        Resolved from the project's already-loaded members rather than a
        per-row query — the triage list is exactly where an N+1 would bite.
        """
        if project is None:
            return None
        if getattr(s, "is_anonymous", False):
            return TrustTier.ANONYMOUS
        submitter = CurrentUser(id=s.user_id, email=s.user_email, name=s.user_name)
        if members_by_user_id is not None:
            return self.trust.resolve_tier_for_member(submitter, members_by_user_id.get(s.user_id))
        return self.trust.resolve_tier(project, submitter)

    async def _build_summary(
        self,
        s: SuggestionSession,
        project: Project | None = None,
        pull_requests: dict[UUID, "PullRequest"] | None = None,
        submitter_tier: TrustTier | None = None,
    ) -> SuggestionSessionSummary:
        """Build a SuggestionSessionSummary from a session model."""
        pr_url = None
        github_pr_url = None
        if s.pr_id:
            if pull_requests is None:
                from ontokit.models.pull_request import PullRequest

                pr_result = await self.db.execute(
                    select(PullRequest).where(PullRequest.id == s.pr_id)
                )
                pr = pr_result.scalar_one_or_none()
            else:
                pr = pull_requests.get(s.pr_id)
            if pr:
                pr_url = pr.github_pr_url
                github_pr_url = pr_url

        # For anonymous sessions, prefer submitter_name/email (credit info collected at submit)
        # over the generic user_name/email set at session creation.
        is_anonymous = getattr(s, "is_anonymous", False)
        if is_anonymous:
            submitter_name = getattr(s, "submitter_name", None) or s.user_name or "Anonymous"
            submitter_email = getattr(s, "submitter_email", None) or s.user_email
        else:
            submitter_name = s.user_name
            submitter_email = s.user_email

        submitter = SuggestionUser(id=s.user_id, name=submitter_name, email=submitter_email)
        reviewer = None
        if s.reviewer_id:
            reviewer = SuggestionUser(
                id=s.reviewer_id, name=s.reviewer_name, email=s.reviewer_email
            )

        return SuggestionSessionSummary(
            session_id=s.session_id,
            branch=s.branch,
            changes_count=s.changes_count,
            last_activity=s.last_activity,
            entities_modified=self._parse_entities_modified(s),
            status=s.status,
            pr_number=s.pr_number,
            pr_url=pr_url,
            github_pr_url=github_pr_url,
            submitter=submitter,
            reviewer=reviewer,
            reviewer_feedback=s.reviewer_feedback,
            reviewed_at=s.reviewed_at,
            revision=s.revision,
            summary=s.summary,
            is_anonymous=is_anonymous,
            submitter_tier=(
                submitter_tier if submitter_tier is not None else self._summary_tier(project, s)
            ),
            is_llm_generated=bool(getattr(s, "is_llm_generated", False)),
            auto_accept_after=getattr(s, "auto_accept_after", None),
            auto_accept_halted_at=getattr(s, "auto_accept_halted_at", None),
        )

    # --- Trust ladder helpers (R5, R6, R11, R12) ---

    def _assert_can_mint(self, project: Project, user: CurrentUser | None) -> None:
        """Refuse entity minting below the trusted rung (R8 / KD4).

        Server-side enforcement. The capabilities endpoint is the matching
        affordance, and both read the same ``resolve_tier``, so the UI and the
        gate can never disagree.
        """
        tier = self.trust.resolve_tier(project, user)
        if self.trust.can_mint_entities(tier):
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "trust_required_to_mint",
                "message": (
                    "Creating new entities requires trusted status on this project. "
                    "Suggest edits to existing entities to earn it."
                ),
                "tier": str(tier),
            },
        )

    async def get_capabilities(
        self, project_id: UUID, user: CurrentUser | None
    ) -> SuggestionCapabilitiesResponse:
        """What the caller may do here, and how trust is earned (AE2).

        Private projects disclose nothing to callers without access — a tier
        readout is itself information about the project.
        """
        member: ProjectMember | None = None
        anonymous_caller = user is None or is_anonymous_user_id(user.id)
        if anonymous_caller:
            project = await self._get_project_record(project_id)
            tier = TrustTier.ANONYMOUS
        else:
            assert user is not None
            loaded = await self.trust.load_project_and_member(project_id, user.id)
            if loaded is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Project not found",
                )
            project, member = loaded
            tier = self.trust.resolve_tier_for_member(user, member)

        is_superadmin = bool(user is not None and user.is_superadmin)
        if not project.is_public and not is_superadmin and member is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This project is not public",
            )

        if anonymous_caller:
            can_suggest = bool(project.is_public)
        else:
            assert user is not None
            can_suggest = self._can_suggest(member.role if member is not None else None, user)

        accepted = 0
        verification_required = False
        if user is not None and not anonymous_caller and can_suggest:
            accepted, total_outcomes = await self.trust.get_outcome_counts(project_id, user.id)
            verification_required = tier is TrustTier.UNTRUSTED and total_outcomes == 0

        return SuggestionCapabilitiesResponse(
            tier=tier,
            can_suggest=can_suggest,
            can_mint_entities=self.trust.can_mint_entities(tier),
            promotion_threshold=project.trust_promotion_threshold or 5,
            accepted_count=accepted,
            auto_accept_enabled=bool(project.auto_accept_enabled),
            auto_accept_quiet_days=project.auto_accept_quiet_days or 7,
            verification_required=verification_required,
        )

    async def _record_terminal_outcome(
        self,
        project: Project,
        session: SuggestionSession,
        outcome: SuggestionOutcomeType,
        decided_by: str | None,
        decided_by_name: str | None,
        note: str | None = None,
    ) -> None:
        """Append the outcome row and, on acceptance, run auto-promotion.

        Deliberately does NOT commit: the caller commits this together with the
        session's status change, so a resolved suggestion can never exist
        without its outcome row (which would silently break promotion counting).
        """
        project_id = project.id
        await self.trust.record_outcome(
            project_id,
            session,
            outcome,
            decided_by,
            note,
            project=project,
            decided_by_name=decided_by_name,
        )

        if outcome is not SuggestionOutcomeType.ACCEPTED:
            return

        promoted = await self.trust.evaluate_promotion(project, session.user_id)
        if not promoted:
            return

        notif = NotificationService(self.db)
        await notif.create_notification(
            user_id=session.user_id,
            notification_type="trust_promoted",
            title=f"You are now a trusted contributor on {project.name}",
            body=(
                "Your suggestions are now reviewed on the trusted queue, and you can "
                "create new entities on this project."
            ),
            project_id=project_id,
            project_name=project.name,
        )

    def _halt_auto_accept(self, session: SuggestionSession) -> None:
        """Stop the quiet-period clock because a reviewer objected (R12)."""
        session.auto_accept_after = None
        session.auto_accept_claimed_until = None
        session.auto_accept_halted_at = datetime.now(UTC)

    async def _schedule_auto_accept(
        self, project_id: UUID, session: SuggestionSession, user: CurrentUser
    ) -> None:
        """Start (or restart) the quiet-period clock if the session is eligible.

        Every write to ``auto_accept_after`` funnels through the single
        eligibility predicate on TrustService, so R13's "LLM output never
        auto-accepts" cannot be bypassed by adding a call site.

        A resolved objection restarts the clock from zero (KTD11) rather than
        resuming the remainder — a reviewer who objected gets a full fresh
        window to look at the revision.
        """
        project = await self._get_project(project_id)
        tier = self.trust.resolve_tier(project, user)
        if not self.trust.is_auto_accept_eligible(project, session, tier):
            session.auto_accept_after = None
            session.auto_accept_claimed_until = None
            return
        quiet_days = project.auto_accept_quiet_days or 7
        session.auto_accept_after = datetime.now(UTC) + timedelta(days=quiet_days)
        session.auto_accept_halted_at = None
        session.auto_accept_claimed_until = None

    def _can_review(self, role: str | None, user: CurrentUser) -> bool:
        """Check if the user's role allows reviewing suggestions."""
        if user.is_superadmin:
            return True
        return role in ("owner", "admin", "editor")

    async def _verify_reviewer_access(self, project_id: UUID, user: CurrentUser) -> Project:
        """Verify the user has editor/admin/owner role, and return the project.

        Returning the loaded project lets callers resolve submitter tiers from
        its members collection without a second fetch — the triage list is
        exactly where an extra query per row would bite.
        """
        project = await self._get_project(project_id)
        role = self._get_user_role(project, user)
        if not self._can_review(role, user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Editor access or above required to review suggestions",
            )
        return project

    async def list_sessions(
        self, project_id: UUID, user: CurrentUser
    ) -> SuggestionSessionListResponse:
        """List suggestion sessions for the current user in a project."""
        result = await self.db.execute(
            select(SuggestionSession)
            .where(
                SuggestionSession.project_id == project_id,
                SuggestionSession.user_id == user.id,
            )
            .order_by(SuggestionSession.last_activity.desc())
        )
        sessions = result.scalars().all()

        items = [await self._build_summary(s) for s in sessions]
        return SuggestionSessionListResponse(items=items)

    async def list_pending(
        self,
        project_id: UUID,
        user: CurrentUser,
        queue: SuggestionQueue | None = None,
    ) -> SuggestionSessionListResponse:
        """List pending suggestion sessions for review (editors/admins).

        ``queue`` splits the list by submitter tier (R9): ``triage`` is the
        anonymous + untrusted rungs, ``review`` is the trusted rung. Omitting it
        returns everything, which keeps existing clients working unchanged.
        """
        project = await self._verify_reviewer_access(project_id, user)

        result = await self.db.execute(
            select(SuggestionSession)
            .where(
                SuggestionSession.project_id == project_id,
                SuggestionSession.status.in_(
                    [
                        SuggestionSessionStatus.SUBMITTED.value,
                        SuggestionSessionStatus.AUTO_SUBMITTED.value,
                    ]
                ),
            )
            .order_by(SuggestionSession.last_activity.desc())
        )
        sessions = result.scalars().all()
        members_by_user_id = {member.user_id: member for member in project.members}
        tiers_by_session_id = {
            session.id: self._summary_tier(project, session, members_by_user_id)
            for session in sessions
        }

        if queue == SuggestionQueue.TRIAGE:
            sessions = [
                session
                for session in sessions
                if tiers_by_session_id[session.id] in (TrustTier.ANONYMOUS, TrustTier.UNTRUSTED)
            ]
        elif queue == SuggestionQueue.REVIEW:
            sessions = [
                session
                for session in sessions
                if tiers_by_session_id[session.id] in (TrustTier.TRUSTED, TrustTier.REVIEWER)
            ]

        pull_requests: dict[UUID, PullRequest] = {}
        pr_ids = [session.pr_id for session in sessions if session.pr_id is not None]
        if pr_ids:
            pr_result = await self.db.execute(select(PullRequest).where(PullRequest.id.in_(pr_ids)))
            pull_requests = {pr.id: pr for pr in pr_result.scalars().all()}

        items = [
            await self._build_summary(
                s,
                project,
                pull_requests,
                tiers_by_session_id[s.id],
            )
            for s in sessions
        ]
        return SuggestionSessionListResponse(items=items)

    async def bulk_review(
        self, project_id: UUID, data: BulkReviewRequest, user: CurrentUser
    ) -> BulkReviewResponse:
        """Accept or dismiss many suggestions in one pass (R9).

        Partial-success by design: each session is processed independently and
        failures are reported per item, because one stale session must never
        abort a forty-item dismissal.
        """
        project = await self._verify_reviewer_access(project_id, user)

        succeeded: list[str] = []
        failed: list[BulkReviewFailure] = []
        for session_id in data.session_ids:
            try:
                if data.action is BulkReviewAction.ACCEPT:
                    await self._approve_unchecked(session_id, user, project)
                else:
                    await self._dismiss_unchecked(project_id, session_id, user, project, data.note)
                succeeded.append(session_id)
            except HTTPException as e:
                await self.db.rollback()
                failed.append(BulkReviewFailure(session_id=session_id, reason=str(e.detail)))
            except Exception as e:  # noqa: BLE001 — one bad row must not abort the batch
                await self.db.rollback()
                logger.warning("Bulk %s failed for session %s: %s", data.action, session_id, e)
                failed.append(BulkReviewFailure(session_id=session_id, reason="Unexpected error"))

        return BulkReviewResponse(action=data.action, succeeded=succeeded, failed=failed)

    async def approve(
        self,
        project_id: UUID,
        session_id: str,
        user: CurrentUser,
        *,
        decided_by: str | None = None,
    ) -> None:
        """Approve a suggestion session — merges the PR.

        ``decided_by`` overrides the recorded actor so the auto-accept sweep can
        attribute its own merges to ``system:auto-accept`` while reusing this
        one path (and therefore the outcome log and promotion evaluation).
        """
        project = await self._verify_reviewer_access(project_id, user)
        await self._approve_unchecked(session_id, user, project, decided_by)

    async def _approve_unchecked(
        self,
        session_id: str,
        user: CurrentUser,
        project: Project,
        decided_by: str | None = None,
    ) -> None:
        """Approve without the reviewer-role gate.

        Internal only. ``approve`` is the authorized entry point; the auto-accept
        sweep is the sole other caller, and its authorization is the trust tier
        re-check it performs immediately before calling in.
        """
        project_id = project.id
        session = await self._get_session(project_id, session_id)

        if session.status not in (
            SuggestionSessionStatus.SUBMITTED.value,
            SuggestionSessionStatus.AUTO_SUBMITTED.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot approve",
            )

        # The PR service commits its merge before returning. If a later session
        # finalization failed, a retry sees the already-merged PR and resumes
        # here without attempting a second external/local merge.
        pr_already_merged = False
        merge_commit_hash: str | None = None
        if session.pr_id is not None:
            pr_result = await self.db.execute(
                select(PullRequest).where(PullRequest.id == session.pr_id)
            )
            linked_pr = pr_result.scalar_one_or_none()
            pr_already_merged = bool(
                linked_pr is not None and linked_pr.status == PRStatus.MERGED.value
            )
            if pr_already_merged and linked_pr is not None:
                merge_commit_hash = linked_pr.merge_commit_hash

        # Merge the PR if it exists and has not already crossed that boundary.
        if session.pr_number and not pr_already_merged:
            from ontokit.schemas.pull_request import PRMergeRequest

            pr_service = get_pull_request_service(self.db)
            try:
                merge_req = PRMergeRequest(
                    merge_message=f"Merge suggestion: {session_id}",
                    delete_source_branch=True,
                )
                merge_response = await pr_service.merge_pull_request(
                    project_id,
                    session.pr_number,
                    merge_req,
                    user,
                    system_auto_accept=(
                        decided_by == SYSTEM_AUTO_ACCEPT_ACTOR
                        and user.id == SYSTEM_AUTO_ACCEPT_ACTOR
                    ),
                )
                merge_commit_hash = merge_response.merge_commit_hash
            except HTTPException:
                logger.warning("PR merge failed for suggestion session %s", session_id)
                raise

        session.status = SuggestionSessionStatus.MERGED.value
        session.reviewer_id = user.id
        session.reviewer_name = user.name
        session.reviewer_email = user.email
        session.reviewed_at = datetime.now(UTC)
        session.last_activity = datetime.now(UTC)
        session.auto_accept_after = None
        session.auto_accept_claimed_until = None
        outcome_actor = decided_by or user.id
        outcome_actor_name = None if outcome_actor == SYSTEM_AUTO_ACCEPT_ACTOR else user.name
        await self._record_terminal_outcome(
            project,
            session,
            SuggestionOutcomeType.ACCEPTED,
            outcome_actor,
            outcome_actor_name,
        )
        await self.db.commit()
        default_branch = self.git_service.get_default_branch(project_id)
        if merge_commit_hash:
            try:
                from ontokit.services.translation_jobs import enqueue_label_diff_after_commit

                await enqueue_label_diff_after_commit(
                    project_id=project_id,
                    branch=default_branch,
                    commit_hash=merge_commit_hash,
                    actor_id=user.id,
                    role=self._get_user_role(project, user) or "viewer",
                )
            except Exception:
                logger.warning("Failed to queue suggestion translation label diff", exc_info=True)
        await self._enqueue_branch_refresh(project_id, default_branch, full_embedding=True)

    async def dismiss(
        self, project_id: UUID, session_id: str, user: CurrentUser, note: str | None = None
    ) -> None:
        """Dismiss a triage-queue suggestion without merging it (R9).

        Distinct from ``reject``: dismissal is the fast skim verdict on junk and
        carries no feedback obligation, where rejection is a considered review
        outcome with a reason the contributor sees.
        """
        project = await self._verify_reviewer_access(project_id, user)
        await self._dismiss_unchecked(project_id, session_id, user, project, note)

    async def _dismiss_unchecked(
        self,
        project_id: UUID,
        session_id: str,
        user: CurrentUser,
        project: Project,
        note: str | None = None,
    ) -> None:
        """Dismiss after the caller has completed the reviewer-role gate."""
        session = await self._get_session(project_id, session_id)

        if session.status not in (
            SuggestionSessionStatus.SUBMITTED.value,
            SuggestionSessionStatus.AUTO_SUBMITTED.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot dismiss",
            )

        session.status = SuggestionSessionStatus.DISCARDED.value
        session.reviewer_id = user.id
        session.reviewer_name = user.name
        session.reviewer_email = user.email
        session.reviewed_at = datetime.now(UTC)
        session.last_activity = datetime.now(UTC)
        self._halt_auto_accept(session)
        await self._record_terminal_outcome(
            project,
            session,
            SuggestionOutcomeType.DISMISSED,
            user.id,
            user.name,
            note,
        )
        await self.db.commit()

    async def reject(
        self, project_id: UUID, session_id: str, data: SuggestionRejectRequest, user: CurrentUser
    ) -> None:
        """Reject a suggestion session with a reason."""
        project = await self._verify_reviewer_access(project_id, user)
        session = await self._get_session(project_id, session_id)

        if session.status not in (
            SuggestionSessionStatus.SUBMITTED.value,
            SuggestionSessionStatus.AUTO_SUBMITTED.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot reject",
            )

        session.status = SuggestionSessionStatus.REJECTED.value
        session.reviewer_id = user.id
        session.reviewer_name = user.name
        session.reviewer_email = user.email
        session.reviewer_feedback = data.reason
        session.reviewed_at = datetime.now(UTC)
        session.last_activity = datetime.now(UTC)
        # An objection halts the quiet-period clock (R12).
        self._halt_auto_accept(session)
        await self._record_terminal_outcome(
            project,
            session,
            SuggestionOutcomeType.REJECTED,
            user.id,
            user.name,
            data.reason,
        )
        await self.db.commit()

    async def request_changes(
        self,
        project_id: UUID,
        session_id: str,
        data: SuggestionRequestChangesRequest,
        user: CurrentUser,
    ) -> None:
        """Request changes on a suggestion session with feedback."""
        await self._verify_reviewer_access(project_id, user)
        session = await self._get_session(project_id, session_id)

        if session.status not in (
            SuggestionSessionStatus.SUBMITTED.value,
            SuggestionSessionStatus.AUTO_SUBMITTED.value,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot request changes",
            )

        session.status = SuggestionSessionStatus.CHANGES_REQUESTED.value
        session.reviewer_id = user.id
        session.reviewer_name = user.name
        session.reviewer_email = user.email
        session.reviewer_feedback = data.feedback
        session.reviewed_at = datetime.now(UTC)
        session.last_activity = datetime.now(UTC)
        # An objection halts the quiet-period clock (R12). No outcome row: the
        # session has not reached a terminal state, it is being revised.
        self._halt_auto_accept(session)
        await self.db.commit()

    async def resubmit(
        self,
        project_id: UUID,
        session_id: str,
        data: SuggestionResubmitRequest,
        user: CurrentUser,
    ) -> SuggestionSubmitResponse:
        """Resubmit a suggestion session after addressing requested changes."""
        session = await self._get_session(project_id, session_id)
        self._verify_ownership(session, user)
        await self._verify_project_access(project_id, user)

        if session.status != SuggestionSessionStatus.CHANGES_REQUESTED.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot resubmit",
            )

        session.status = SuggestionSessionStatus.SUBMITTED.value
        session.revision = (session.revision or 1) + 1
        session.summary = data.summary
        session.reviewer_feedback = None
        session.reviewed_at = None
        session.last_activity = datetime.now(UTC)
        # The objection is resolved: restart the quiet clock from zero (KTD11).
        await self._schedule_auto_accept(project_id, session, user)
        await self.db.commit()

        return SuggestionSubmitResponse(
            pr_number=session.pr_number or 0,
            pr_url=None,
            status="submitted",
        )

    async def discard(self, project_id: UUID, session_id: str, user: CurrentUser) -> None:
        """Discard a suggestion session and delete its branch."""
        session = await self._get_session(project_id, session_id)
        self._verify_ownership(session, user)
        await self._verify_project_access(project_id, user)

        if session.status != SuggestionSessionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot discard",
            )

        # Delete the git branch
        try:
            self.git_service.delete_branch(project_id, session.branch, force=True)
        except Exception as e:
            logger.warning(f"Failed to delete suggestion branch {session.branch}: {e}")

        session.status = SuggestionSessionStatus.DISCARDED.value
        session.last_activity = datetime.now(UTC)
        await self.db.commit()

    async def beacon_save(
        self, project_id: UUID, data: SuggestionBeaconRequest, token: str
    ) -> None:
        """Handle a beacon save (sendBeacon flush) with token-based auth."""
        # Verify the beacon token
        verified_session_id = verify_beacon_token(token)
        if verified_session_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired beacon token",
            )

        if verified_session_id != data.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token does not match session",
            )

        # Look up the session
        session = await self._get_session(project_id, data.session_id)

        if session.status != SuggestionSessionStatus.ACTIVE.value:
            return  # Silently ignore saves to non-active sessions

        # Re-check project access with the session owner's identity
        session_user = CurrentUser(
            id=session.user_id,
            email=session.user_email,
            name=session.user_name,
        )
        await self._verify_project_access(project_id, session_user)

        await self._beacon_flush(project_id, session, data)

    async def beacon_save_anonymous(
        self, project_id: UUID, data: SuggestionBeaconRequest, verified_session_id: str
    ) -> None:
        """Handle a beacon save for an ANONYMOUS session.

        The caller (route) has already verified the X-Anonymous-Token; this
        method binds it to the payload's session and re-checks the session is
        actually anonymous (an anonymous token must never flush an
        authenticated user's session). No user-identity access re-check: the
        session's project is public by construction (create-time gate).
        """
        if verified_session_id != data.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token does not match session",
            )

        session = await self._get_session(project_id, data.session_id)

        if not session.is_anonymous:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Session is not an anonymous session",
            )

        if session.status != SuggestionSessionStatus.ACTIVE.value:
            return  # Silently ignore saves to non-active sessions

        await self._beacon_flush(project_id, session, data)

    async def _beacon_flush(
        self, project_id: UUID, session: SuggestionSession, data: SuggestionBeaconRequest
    ) -> None:
        """Commit a beacon payload to the session branch (fire-and-forget)."""
        content = data.content.encode("utf-8")
        if session.is_anonymous:
            try:
                self._check_anonymous_write_budget(session, content)
            except HTTPException as exc:
                logger.info(
                    "Beacon save refused for anonymous session %s: %s",
                    session.session_id,
                    exc.detail,
                )
                return

        project = await self._get_project(project_id)
        filename = self._get_git_ontology_path(project)
        actor = None
        if not bool(getattr(session, "is_anonymous", False)):
            actor = CurrentUser(
                id=session.user_id,
                email=session.user_email,
                name=session.user_name,
            )
        author_name, author_email = await self.commit_identity.resolve(
            session.user_id,
            session.user_name,
            is_anonymous=bool(getattr(session, "is_anonymous", False)),
            session_id=session.session_id,
        )

        # Serialize git writes per branch to prevent lost commits
        async with branch_write_lock(self.db, project_id, session.branch):
            await self.db.refresh(session)
            if session.status != SuggestionSessionStatus.ACTIVE.value:
                return
            anonymous_write_bytes: int | None = None
            if session.is_anonymous:
                try:
                    anonymous_write_bytes = self._check_anonymous_write_budget(
                        session, content
                    )
                except HTTPException as exc:
                    logger.info(
                        "Beacon save refused for anonymous session %s: %s",
                        session.session_id,
                        exc.detail,
                    )
                    return
            current_content = self.git_service.get_file_from_branch(
                project_id, session.branch, filename
            )
            self._assert_branch_content_can_mint(project, actor, current_content, data.content)
            try:
                self.git_service.commit_to_branch(  # type: ignore[attr-defined]
                    project_id=project_id,
                    branch_name=session.branch,
                    ontology_content=content,
                    filename=filename,
                    message="Auto-save (beacon)",
                    author_name=author_name,
                    author_email=author_email,
                )
            except Exception as e:
                logger.warning(f"Beacon save failed for session {data.session_id}: {e}")
                return  # Beacon is fire-and-forget

            session.changes_count += 1
            if anonymous_write_bytes is not None:
                session.anonymous_content_bytes += anonymous_write_bytes
            session.last_activity = datetime.now(UTC)
            await self.db.commit()

    # --- Anonymous session methods ---

    async def create_anonymous_session(
        self, project_id: UUID, client_ip: str
    ) -> AnonymousSessionCreateResponse:
        """Create an anonymous suggestion session with rate limiting.

        Checks that fewer than 5 anonymous sessions have been created from
        the same IP address in the last hour before creating a new one.
        """
        from sqlalchemy import func as sa_func

        # Verify project exists AND is public — anonymous users must never be
        # able to create suggestion branches/PRs against a private project.
        project = await self._get_project(project_id)
        if not project.is_public:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Anonymous suggestions are only available on public projects",
            )

        # Rate limit check: max 5 anonymous sessions per IP per hour — GLOBAL
        # across projects on purpose. Scoping by project would let one IP mint
        # 5 sessions (and git branches) on every public project per hour.
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        rate_result = await self.db.execute(
            select(sa_func.count(SuggestionSession.id)).where(
                SuggestionSession.is_anonymous.is_(True),
                SuggestionSession.client_ip == client_ip,
                SuggestionSession.created_at > cutoff,
            )
        )
        session_count = rate_result.scalar() or 0
        if session_count >= 5:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
            )

        # Generate identifiers
        session_id = f"s_{secrets.token_hex(8)}"
        branch = f"suggest/anonymous/{session_id}"
        anonymous_token = create_anonymous_token(session_id)
        beacon_token = create_beacon_token(session_id)
        anon_user_id = f"anonymous-{secrets.token_hex(6)}"

        # Create the git branch
        try:
            self.git_service.create_branch(project_id, branch)
        except Exception as e:
            logger.error(f"Failed to create anonymous suggestion branch: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create suggestion branch",
            ) from e

        # Create the database record
        db_session = SuggestionSession(
            project_id=project_id,
            user_id=anon_user_id,
            user_name="Anonymous",
            user_email=None,
            session_id=session_id,
            branch=branch,
            beacon_token=beacon_token,
            is_anonymous=True,
            client_ip=client_ip,
        )
        try:
            self.db.add(db_session)
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            try:
                self.git_service.delete_branch(project_id, branch, force=True)
            except Exception:
                logger.warning(f"Failed to clean up orphaned anonymous branch {branch}")
            raise

        try:
            await self.db.refresh(db_session)
        except Exception:
            logger.warning("Anonymous session %s committed but refresh failed", session_id)
            re_result = await self.db.execute(
                select(SuggestionSession).where(
                    SuggestionSession.project_id == project_id,
                    SuggestionSession.session_id == session_id,
                )
            )
            db_session = re_result.scalar_one()

        return AnonymousSessionCreateResponse(
            session_id=db_session.session_id,
            branch=db_session.branch,
            created_at=db_session.created_at,
            anonymous_token=anonymous_token,
        )

    async def save_anonymous(
        self,
        project_id: UUID,
        session_id: str,
        data: SuggestionSaveRequest,
        verified_session_id: str,
    ) -> SuggestionSaveResponse:
        """Save content to an anonymous suggestion session branch."""
        session = await self._get_session(project_id, session_id)

        # Verify the token belongs to this session
        if verified_session_id != session.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token does not match session",
            )
        if not session.is_anonymous:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Session is not an anonymous session",
            )
        if session.status != SuggestionSessionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot save",
            )

        content = data.content.encode("utf-8")
        self._check_anonymous_write_budget(session, content)

        project = await self._get_project(project_id)
        filename = self._get_git_ontology_path(project)

        # R14: the credit name the submitter typed is used for the NAME only —
        # never for the address, which is a per-session anonymous alias.
        author_name, author_email = await self.commit_identity.resolve(
            session.user_id,
            session.user_name,
            is_anonymous=True,
            session_id=session.session_id,
        )

        async with branch_write_lock(self.db, project_id, session.branch):
            await self.db.refresh(session)
            if session.status != SuggestionSessionStatus.ACTIVE.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Session is {session.status}, cannot save",
                )
            content_bytes = self._check_anonymous_write_budget(session, content)
            current_content = self.git_service.get_file_from_branch(
                project_id, session.branch, filename
            )
            self._assert_branch_content_can_mint(project, None, current_content, data.content)
            commit_message = f"Update {data.entity_label}"
            try:
                commit_info = self.git_service.commit_to_branch(  # type: ignore[attr-defined]
                    project_id=project_id,
                    branch_name=session.branch,
                    ontology_content=content,
                    filename=filename,
                    message=commit_message,
                    author_name=author_name,
                    author_email=author_email,
                )
            except Exception as e:
                logger.error(f"Failed to save anonymous suggestion: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to save suggestion to branch",
                ) from e

            session.changes_count += 1
            session.anonymous_content_bytes += content_bytes
            self._update_entities_modified(session, data.entity_label)
            session.last_activity = datetime.now(UTC)
            try:
                await self.db.commit()
            except Exception as e:
                await self.db.rollback()
                logger.error(
                    "Failed to update anonymous session metadata: session=%s error=%s",
                    session.session_id,
                    e,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Saved to branch but failed to update session metadata",
                ) from e

        return SuggestionSaveResponse(
            commit_hash=commit_info.hash,
            branch=session.branch,
            changes_count=session.changes_count,
        )

    async def submit_anonymous(
        self,
        project_id: UUID,
        session_id: str,
        data: AnonymousSubmitRequest,
        verified_session_id: str,
    ) -> AnonymousSubmitResponse:
        """Submit an anonymous suggestion session as a pull request."""
        session = await self._get_session(project_id, session_id)

        if verified_session_id != session.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token does not match session",
            )
        if not session.is_anonymous:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Session is not an anonymous session",
            )
        if session.status != SuggestionSessionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot submit",
            )
        if session.changes_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No changes to submit",
            )

        # Store optional credit info
        if data.submitter_name or data.submitter_email:
            session.submitter_name = data.submitter_name
            session.submitter_email = data.submitter_email
            # Update user_name so the PR description shows the provided credit name
            session.user_name = data.submitter_name or "Anonymous"

        mock_user = CurrentUser(
            id=session.user_id,
            name=session.user_name or "Anonymous",
            email=session.submitter_email,
        )

        result = await self._create_pr_for_session(
            project_id, session, mock_user, data.summary, "submitted"
        )

        return AnonymousSubmitResponse(
            pr_number=result.pr_number,
            pr_url=result.pr_url,
            status=result.status,
        )

    async def discard_anonymous(
        self,
        project_id: UUID,
        session_id: str,
        verified_session_id: str,
    ) -> None:
        """Discard an anonymous suggestion session and delete its branch."""
        session = await self._get_session(project_id, session_id)

        if verified_session_id != session.session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token does not match session",
            )
        if not session.is_anonymous:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Session is not an anonymous session",
            )
        if session.status != SuggestionSessionStatus.ACTIVE.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Session is {session.status}, cannot discard",
            )

        try:
            self.git_service.delete_branch(project_id, session.branch, force=True)
        except Exception as e:
            logger.warning(f"Failed to delete anonymous suggestion branch {session.branch}: {e}")

        session.status = SuggestionSessionStatus.DISCARDED.value
        session.last_activity = datetime.now(UTC)
        await self.db.commit()

    async def auto_submit_stale_sessions(self) -> int:
        """Auto-create PRs for stale suggestion sessions.

        Returns the number of sessions auto-submitted.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=30)

        result = await self.db.execute(
            select(SuggestionSession).where(
                SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
                SuggestionSession.changes_count > 0,
                SuggestionSession.last_activity < cutoff,
                # Anonymous sessions are handled by reap_stale_anonymous_sessions:
                # their pseudo-user is never a project member, so the access
                # re-check below would always discard them WITHOUT deleting the
                # branch (orphaned-branch leak).
                SuggestionSession.is_anonymous.is_(False),
            )
        )
        stale_sessions = result.scalars().all()

        count = 0
        for session in stale_sessions:
            mock_user = CurrentUser(
                id=session.user_id,
                email=session.user_email,
                name=session.user_name,
            )

            # A background sweep cannot satisfy the interactive verification
            # challenge or account limiter. Leave untrusted work ACTIVE so its
            # owner must submit through the normal gated endpoint.
            try:
                project = await self._verify_project_access(session.project_id, mock_user)
            except HTTPException:
                session.status = SuggestionSessionStatus.DISCARDED.value
                await self.db.commit()
                logger.warning(
                    "Discarded session %s: user %s lost project access",
                    session.session_id,
                    session.user_id,
                )
                continue
            if self.trust.resolve_tier(project, mock_user) is TrustTier.UNTRUSTED:
                logger.info(
                    "Skipped stale auto-submit for untrusted session %s",
                    session.session_id,
                )
                continue

            # Atomically claim the session to prevent concurrent workers from
            # processing the same session.  Only proceed if this UPDATE affects
            # exactly one row (i.e. no other worker claimed it first).
            claim_result = await self.db.execute(
                update(SuggestionSession)
                .where(
                    SuggestionSession.id == session.id,
                    SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
                    SuggestionSession.changes_count > 0,
                    SuggestionSession.last_activity < cutoff,
                    SuggestionSession.is_anonymous.is_(False),
                )
                .values(status=SuggestionSessionStatus.AUTO_SUBMITTED.value)
            )
            if claim_result.rowcount != 1:  # type: ignore[attr-defined]
                continue  # Another worker already claimed this session
            await self.db.commit()
            # Refresh so the in-memory object reflects the new status
            await self.db.refresh(session)

            try:
                await self._create_pr_for_session(
                    session.project_id,
                    session,
                    mock_user,
                    summary="Auto-submitted: session inactive for 30+ minutes.",
                    new_status=SuggestionSessionStatus.AUTO_SUBMITTED.value,
                )
                count += 1
                logger.info(
                    f"Auto-submitted suggestion session {session.session_id} "
                    f"for project {session.project_id}"
                )
            except Exception as e:
                logger.error(f"Failed to auto-submit session {session.session_id}: {e}")
                # Rollback any failed transaction state before reverting the claim
                await self.db.rollback()
                try:
                    session.status = SuggestionSessionStatus.ACTIVE.value
                    await self.db.commit()
                except Exception as revert_err:
                    logger.error(
                        f"Failed to revert session {session.session_id} to ACTIVE: {revert_err}"
                    )

        return count

    async def auto_accept_ripe_sessions(self) -> int:
        """Merge trusted suggestions whose quiet period has elapsed (R11).

        Multi-instance safe: each session is claimed with a conditional UPDATE
        carrying the full predicate, and only a rowcount of exactly 1 proceeds —
        the same pattern ``auto_submit_stale_sessions`` uses (R17).

        Tier is re-verified at merge time as well as at scheduling time: a
        contributor whose trust was revoked while the clock ran must not
        auto-merge. The clock is the braces; this is the belt.

        Returns the number of sessions auto-merged.
        """
        now = datetime.now(UTC)
        lease_until = now + AUTO_ACCEPT_LEASE

        result = await self.db.execute(
            select(SuggestionSession)
            .where(
                SuggestionSession.status.in_(
                    [
                        SuggestionSessionStatus.SUBMITTED.value,
                        SuggestionSessionStatus.AUTO_SUBMITTED.value,
                    ]
                ),
                SuggestionSession.auto_accept_after.is_not(None),
                SuggestionSession.auto_accept_after <= now,
                SuggestionSession.auto_accept_halted_at.is_(None),
                SuggestionSession.is_anonymous.is_(False),
                SuggestionSession.is_llm_generated.is_(False),
                or_(
                    SuggestionSession.auto_accept_claimed_until.is_(None),
                    SuggestionSession.auto_accept_claimed_until <= now,
                ),
            )
            .order_by(SuggestionSession.auto_accept_after.asc(), SuggestionSession.id.asc())
            .limit(AUTO_ACCEPT_BATCH_SIZE)
        )
        ripe = result.scalars().all()

        count = 0
        for session in ripe:
            # Atomically claim with an expiring lease. Keeping the original
            # schedule makes the claim recoverable if this worker dies after
            # committing the claim but before finalization.
            claim_result = await self.db.execute(
                update(SuggestionSession)
                .where(
                    SuggestionSession.id == session.id,
                    SuggestionSession.status.in_(
                        [
                            SuggestionSessionStatus.SUBMITTED.value,
                            SuggestionSessionStatus.AUTO_SUBMITTED.value,
                        ]
                    ),
                    SuggestionSession.auto_accept_after.is_not(None),
                    SuggestionSession.auto_accept_after <= now,
                    SuggestionSession.auto_accept_halted_at.is_(None),
                    SuggestionSession.is_anonymous.is_(False),
                    SuggestionSession.is_llm_generated.is_(False),
                    or_(
                        SuggestionSession.auto_accept_claimed_until.is_(None),
                        SuggestionSession.auto_accept_claimed_until <= now,
                    ),
                )
                .values(auto_accept_claimed_until=lease_until)
            )
            if claim_result.rowcount != 1:  # type: ignore[attr-defined]
                continue  # Another worker claimed it first
            await self.db.commit()

            project = await self._get_project(session.project_id)
            submitter = CurrentUser(
                id=session.user_id, email=session.user_email, name=session.user_name
            )
            tier = self.trust.resolve_tier(project, submitter)
            if not self.trust.is_auto_accept_eligible(project, session, tier):
                session.auto_accept_after = None
                session.auto_accept_claimed_until = None
                await self.db.commit()
                logger.info(
                    "Cancelled auto-accept for session %s: eligibility changed for submitter %s",
                    session.session_id,
                    session.user_id,
                )
                continue

            # Merge through the normal approve path so the outcome row,
            # promotion evaluation and PR merge all still happen. The reviewer
            # gate is skipped deliberately: the tier re-check above IS this
            # path's authorization.
            system_actor = CurrentUser(
                id=SYSTEM_AUTO_ACCEPT_ACTOR,
                email=None,
                name="OntoKit auto-accept",
            )
            try:
                await self._approve_unchecked(
                    session.session_id,
                    system_actor,
                    project,
                    SYSTEM_AUTO_ACCEPT_ACTOR,
                )
                count += 1
                logger.info(
                    "Auto-accepted suggestion session %s for project %s after the quiet period",
                    session.session_id,
                    session.project_id,
                )
            except Exception as e:
                logger.error("Failed to auto-accept session %s: %s", session.session_id, e)
                await self.db.rollback()
                # Revert the claim so the next sweep retries.
                try:
                    session.auto_accept_claimed_until = None
                    await self.db.commit()
                except Exception as revert_err:
                    logger.error(
                        "Failed to revert auto-accept claim for session %s: %s",
                        session.session_id,
                        revert_err,
                    )

        return count

    async def reap_stale_anonymous_sessions(self, ttl_hours: int = 24) -> int:
        """Discard stale ANONYMOUS sessions and delete their git branches.

        Anonymous tokens expire after 24h, so past the TTL the session is
        unreachable by its creator anyway. Without this reaper every abandoned
        anonymous session leaves an orphaned git branch forever (the authed
        sweep can't handle them: the anonymous pseudo-user is never a project
        member). Includes sessions with changes_count == 0 — those were never
        matched by any sweep at all.

        Returns the number of sessions reaped.
        """
        cutoff = datetime.now(UTC) - timedelta(hours=ttl_hours)

        result = await self.db.execute(
            select(SuggestionSession).where(
                SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
                SuggestionSession.is_anonymous.is_(True),
                SuggestionSession.last_activity < cutoff,
            )
        )
        stale = result.scalars().all()

        count = 0
        for session in stale:
            # Atomic claim (same pattern as auto_submit_stale_sessions)
            claim_result = await self.db.execute(
                update(SuggestionSession)
                .where(
                    SuggestionSession.id == session.id,
                    SuggestionSession.status == SuggestionSessionStatus.ACTIVE.value,
                    SuggestionSession.is_anonymous.is_(True),
                    SuggestionSession.last_activity < cutoff,
                )
                .values(status=SuggestionSessionStatus.DISCARDED.value)
            )
            if claim_result.rowcount != 1:  # type: ignore[attr-defined]
                continue
            await self.db.commit()

            try:
                self.git_service.delete_branch(session.project_id, session.branch, force=True)
            except Exception as e:
                # Branch may already be gone; log and keep the discard.
                logger.warning(
                    "Reaped anonymous session %s but branch delete failed: %s",
                    session.session_id,
                    e,
                )
            count += 1
            logger.info(
                "Reaped stale anonymous session %s (changes_count=%s)",
                session.session_id,
                session.changes_count,
            )

        return count


def get_suggestion_service(db: AsyncSession) -> SuggestionService:
    """Factory function for dependency injection."""
    return SuggestionService(db)
