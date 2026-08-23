"""Composite duplicate detection service — exact + semantic + structural scoring."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.duplicate_check import (
    CandidateSource,
    DistinctDecisionMarkRequest,
    DistinctDecisionResponse,
    DuplicateCandidate,
    DuplicateCheckResponse,
    DuplicateVerdict,
    ScoreBreakdown,
)
from ontokit.services.embedding_service import EmbeddingService
from ontokit.services.structural_similarity_service import StructuralSimilarityService

logger = logging.getLogger(__name__)

# D-01 weights
EXACT_WEIGHT = 0.40
SEMANTIC_WEIGHT = 0.40
STRUCTURAL_WEIGHT = 0.20

# D-02 thresholds
BLOCK_THRESHOLD = 0.95
WARN_THRESHOLD = 0.80


class DuplicateCandidateUnavailableError(ValueError):
    """The requested pair is not a current warn/block duplicate candidate."""


class DistinctDecisionConflictError(RuntimeError):
    """A concurrent writer recorded a different active decision for the pair."""


class DuplicateCheckService:
    """Composite duplicate detection per D-01/D-02/D-03."""

    def __init__(self, db: AsyncSession):
        self._db = db
        self._embedding_svc = EmbeddingService(db)
        self._structural_svc = StructuralSimilarityService()

    async def check(
        self,
        project_id: UUID,
        label: str,
        entity_type: str = "class",
        parent_iri: str | None = None,
        limit: int = 10,
        billing_user_id: str = "system:duplicate-check",
        exclude_branch: str | None = None,
        exclude_iris: set[str] | None = None,
        proposed_iri: str | None = None,
        suggestion_session_id: UUID | None = None,
        suppress_distinct: bool = True,
    ) -> DuplicateCheckResponse:
        """Run composite duplicate check across all branches (DEDUP-04 through DEDUP-08).

        Scoring:
          - exact: 40% — case-insensitive label match against ontology index
          - semantic: 40% — embedding cosine similarity via all-branch ANN search
          - structural: 20% — folio-python Jaccard parent similarity

        ``entity_type`` mirrors ``DuplicateCheckRequest.entity_type``; scoring is
        currently type-agnostic (candidates come from the shared index), the
        parameter is accepted for API stability.

        Returns verdict (block/warn/pass), composite score, breakdown, and enriched candidates.
        """
        normalized_label = label.lower().strip()
        del suggestion_session_id  # carried for audit-capable callers; check remains read-only

        # 1. Semantic search across ALL branches (DEDUP-08)
        semantic_candidates = await self._embedding_svc.semantic_search_all_branches(
            project_id,
            label,
            limit=limit,
            billing_user_id=billing_user_id,
            exclude_branch=exclude_branch,
            exclude_iris=exclude_iris,
        )

        if not semantic_candidates:
            return DuplicateCheckResponse(
                verdict="pass",
                composite_score=0.0,
                score_breakdown=ScoreBreakdown(exact=0.0, semantic=0.0, structural=0.0),
                candidates=[],
            )

        # 2. Build candidate list with all three scores
        candidates: list[DuplicateCandidate] = []
        suppressed_decisions: list[DistinctDecisionResponse] = []
        best_composite = 0.0
        best_breakdown = ScoreBreakdown(exact=0.0, semantic=0.0, structural=0.0)

        for sem_result in semantic_candidates:
            # Exact score: case-insensitive label match
            candidate_label_norm = (sem_result.label or "").lower().strip()
            exact_score = 1.0 if candidate_label_norm == normalized_label else 0.0

            # Semantic score: from ANN search
            semantic_score = sem_result.score

            # Structural score: folio-python Jaccard (returns 0.0 if folio unavailable)
            structural_result = (
                self._structural_svc.try_compute_similarity(
                    sem_result.iri,
                    parent_iri,
                    max_depth=3,
                )
                if parent_iri
                else None
            )
            structural_available = structural_result is not None
            structural_score = structural_result or 0.0

            # Composite (D-01 weights). A freshly-minted proposal frequently has
            # no structural context. In that case, renormalize the two available
            # signals instead of treating the missing signal as evidence against
            # duplication. Thresholds remain unchanged, so weak semantic matches
            # are not promoted into false blocks.
            if structural_available:
                composite = (
                    EXACT_WEIGHT * exact_score
                    + SEMANTIC_WEIGHT * semantic_score
                    + STRUCTURAL_WEIGHT * structural_score
                )
            elif exact_score == 0.0:
                composite = semantic_score
            else:
                composite = (EXACT_WEIGHT * exact_score + SEMANTIC_WEIGHT * semantic_score) / (
                    EXACT_WEIGHT + SEMANTIC_WEIGHT
                )

            # An exact normalized label match is itself deterministic duplicate
            # evidence. Do not let representation differences between a short
            # query and the candidate's richer embedding text dilute that fact
            # to the strict non-blocking threshold boundary.
            if exact_score == 1.0:
                composite = 1.0

            if proposed_iri and suppress_distinct and proposed_iri != sem_result.iri:
                iri_a, iri_b, fingerprint_a, fingerprint_b = self._fingerprints_for_pair(
                    proposed_iri=proposed_iri,
                    proposed_label=label,
                    entity_type=entity_type,
                    parent_iri=parent_iri,
                    candidate_iri=sem_result.iri,
                    candidate_label=sem_result.label or "",
                    candidate_entity_type=sem_result.entity_type,
                )
                decision = await self._find_matching_decision(
                    project_id,
                    iri_a,
                    iri_b,
                    fingerprint_a,
                    fingerprint_b,
                )
                if decision is not None:
                    suppressed_decisions.append(DistinctDecisionResponse.model_validate(decision))
                    continue

            # Determine source (D-09)
            source = await self._classify_source(project_id, sem_result.branch)

            candidates.append(
                DuplicateCandidate(
                    iri=sem_result.iri,
                    label=sem_result.label or "",
                    entity_type=sem_result.entity_type,
                    score=round(composite, 4),
                    source=source,
                    branch=sem_result.branch,
                )
            )

            if composite > best_composite:
                best_composite = composite
                best_breakdown = ScoreBreakdown(
                    exact=round(exact_score, 4),
                    semantic=round(semantic_score, 4),
                    structural=round(structural_score, 4),
                )

        # Sort candidates by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)

        # Determine verdict (D-02)
        if best_composite > BLOCK_THRESHOLD:
            verdict: DuplicateVerdict = "block"
        elif best_composite > WARN_THRESHOLD:
            verdict = "warn"
        else:
            verdict = "pass"

        return DuplicateCheckResponse(
            verdict=verdict,
            composite_score=round(best_composite, 4),
            score_breakdown=best_breakdown,
            candidates=candidates,
            suppressed_decisions=suppressed_decisions,
        )

    async def mark_distinct(
        self,
        project_id: UUID,
        request: DistinctDecisionMarkRequest,
        actor_id: str,
        billing_user_id: str,
    ) -> DuplicateRejection:
        """Record a fingerprint-bound distinct decision, preserving prior history."""
        if request.proposed_iri == request.candidate_iri:
            raise ValueError("An entity cannot be marked distinct from itself")
        await self._validate_suggestion_session(project_id, request.suggestion_session_id)

        check = await self.check(
            project_id=project_id,
            label=request.label,
            entity_type=request.entity_type,
            parent_iri=request.parent_iri,
            proposed_iri=request.proposed_iri,
            suggestion_session_id=request.suggestion_session_id,
            billing_user_id=billing_user_id,
            suppress_distinct=False,
        )
        candidate = next(
            (item for item in check.candidates if item.iri == request.candidate_iri), None
        )
        if candidate is None or candidate.score <= WARN_THRESHOLD:
            raise DuplicateCandidateUnavailableError(
                "The selected entity is no longer a duplicate warning"
            )

        iri_a, iri_b, fingerprint_a, fingerprint_b = self._fingerprints_for_pair(
            proposed_iri=request.proposed_iri,
            proposed_label=request.label,
            entity_type=request.entity_type,
            parent_iri=request.parent_iri,
            candidate_iri=candidate.iri,
            candidate_label=candidate.label,
            candidate_entity_type=candidate.entity_type,
        )
        now = datetime.now(UTC)
        active = await self._get_active_decision(project_id, iri_a, iri_b, for_update=True)
        if active is not None and (
            active.fingerprint_a == fingerprint_a and active.fingerprint_b == fingerprint_b
        ):
            return active

        if active is not None:
            active.revoked_at = now
            active.revoked_by = actor_id

        decision = DuplicateRejection(
            project_id=project_id,
            iri_a=iri_a,
            iri_b=iri_b,
            fingerprint_a=fingerprint_a,
            fingerprint_b=fingerprint_b,
            reason=request.reason,
            marked_by=actor_id,
            suggestion_session_id=request.suggestion_session_id,
        )
        try:
            async with self._db.begin_nested():
                self._db.add(decision)
                await self._db.flush()
        except IntegrityError as exc:
            concurrent = await self._get_active_decision(project_id, iri_a, iri_b)
            if concurrent is not None and (
                concurrent.fingerprint_a == fingerprint_a
                and concurrent.fingerprint_b == fingerprint_b
            ):
                return concurrent
            raise DistinctDecisionConflictError(
                "A different distinct decision was recorded concurrently; retry"
            ) from exc

        if active is not None:
            active.superseded_by_id = decision.id
        await self._db.commit()
        await self._db.refresh(decision)
        return decision

    async def list_distinct_decisions(
        self,
        project_id: UUID,
        *,
        include_inactive: bool = False,
        skip: int = 0,
        limit: int = 50,
    ) -> list[DuplicateRejection]:
        statement = select(DuplicateRejection).where(DuplicateRejection.project_id == project_id)
        if not include_inactive:
            statement = statement.where(DuplicateRejection.revoked_at.is_(None))
        result = await self._db.execute(
            statement.order_by(DuplicateRejection.marked_at.desc()).offset(skip).limit(limit)
        )
        return list(result.scalars().all())

    async def revoke_distinct_decision(
        self, project_id: UUID, decision_id: UUID, actor_id: str
    ) -> DuplicateRejection | None:
        result = await self._db.execute(
            select(DuplicateRejection)
            .where(
                DuplicateRejection.id == decision_id,
                DuplicateRejection.project_id == project_id,
            )
            .with_for_update()
        )
        decision = result.scalar_one_or_none()
        if decision is None:
            return None
        if decision.revoked_at is None:
            decision.revoked_at = datetime.now(UTC)
            decision.revoked_by = actor_id
            await self._db.commit()
            await self._db.refresh(decision)
        return decision

    async def _find_matching_decision(
        self,
        project_id: UUID,
        iri_a: str,
        iri_b: str,
        fingerprint_a: str,
        fingerprint_b: str,
    ) -> DuplicateRejection | None:
        result = await self._db.execute(
            select(DuplicateRejection).where(
                DuplicateRejection.project_id == project_id,
                DuplicateRejection.iri_a == iri_a,
                DuplicateRejection.iri_b == iri_b,
                DuplicateRejection.fingerprint_a == fingerprint_a,
                DuplicateRejection.fingerprint_b == fingerprint_b,
                DuplicateRejection.revoked_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _get_active_decision(
        self, project_id: UUID, iri_a: str, iri_b: str, *, for_update: bool = False
    ) -> DuplicateRejection | None:
        statement = select(DuplicateRejection).where(
            DuplicateRejection.project_id == project_id,
            DuplicateRejection.iri_a == iri_a,
            DuplicateRejection.iri_b == iri_b,
            DuplicateRejection.revoked_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._db.execute(statement)
        return result.scalar_one_or_none()

    async def _validate_suggestion_session(self, project_id: UUID, session_id: UUID | None) -> None:
        if session_id is None:
            return
        result = await self._db.execute(
            select(SuggestionSession.id).where(
                SuggestionSession.id == session_id,
                SuggestionSession.project_id == project_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise ValueError("Suggestion session does not belong to this project")

    @classmethod
    def _fingerprints_for_pair(
        cls,
        *,
        proposed_iri: str,
        proposed_label: str,
        entity_type: str,
        parent_iri: str | None,
        candidate_iri: str,
        candidate_label: str,
        candidate_entity_type: str,
    ) -> tuple[str, str, str, str]:
        proposed_snapshot = {
            "iri": cls._normalize_iri(proposed_iri),
            "label": cls._normalize_text(proposed_label),
            "entity_type": cls._normalize_text(entity_type),
            "parent_iri": cls._normalize_iri(parent_iri),
        }
        candidate_snapshot = {
            "iri": cls._normalize_iri(candidate_iri),
            "label": cls._normalize_text(candidate_label),
            "entity_type": cls._normalize_text(candidate_entity_type),
            "parent_iri": None,
        }
        proposed_key = str(proposed_snapshot["iri"])
        candidate_key = str(candidate_snapshot["iri"])
        if proposed_key < candidate_key:
            return (
                proposed_key,
                candidate_key,
                cls._fingerprint(proposed_snapshot),
                cls._fingerprint(candidate_snapshot),
            )
        return (
            candidate_key,
            proposed_key,
            cls._fingerprint(candidate_snapshot),
            cls._fingerprint(proposed_snapshot),
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(value.split()).casefold()

    @staticmethod
    def _normalize_iri(value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @staticmethod
    def _fingerprint(snapshot: dict[str, str | None]) -> str:
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    async def _classify_source(self, project_id: UUID, branch: str) -> CandidateSource:
        """Classify a branch as main/pending/rejected per D-09."""
        # Check if branch matches a suggestion session
        session = (
            (
                await self._db.execute(
                    select(SuggestionSession)
                    .where(
                        SuggestionSession.project_id == project_id,
                        SuggestionSession.branch == branch,
                    )
                    .order_by(SuggestionSession.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

        if not session:
            return "main"

        status = session.status
        if status == SuggestionSessionStatus.REJECTED.value:
            return "rejected"
        elif status in (
            SuggestionSessionStatus.ACTIVE.value,
            SuggestionSessionStatus.SUBMITTED.value,
            SuggestionSessionStatus.AUTO_SUBMITTED.value,
            SuggestionSessionStatus.CHANGES_REQUESTED.value,
        ):
            return "pending"
        else:
            return "main"  # merged/discarded sessions are now part of main
