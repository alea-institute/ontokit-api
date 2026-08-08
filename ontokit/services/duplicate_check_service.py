"""Composite duplicate detection service — exact + semantic + structural scoring."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.duplicate_check import (
    CandidateSource,
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
        del entity_type  # accepted for schema parity; scoring is type-agnostic today
        normalized_label = label.lower().strip()

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
            available_weight = EXACT_WEIGHT + SEMANTIC_WEIGHT
            if structural_available:
                available_weight += STRUCTURAL_WEIGHT
            composite = (
                EXACT_WEIGHT * exact_score
                + SEMANTIC_WEIGHT * semantic_score
                + (STRUCTURAL_WEIGHT * structural_score if structural_available else 0.0)
            ) / available_weight

            # An exact normalized label match is itself deterministic duplicate
            # evidence. Do not let representation differences between a short
            # query and the candidate's richer embedding text dilute that fact
            # to the strict non-blocking threshold boundary.
            if exact_score == 1.0:
                composite = 1.0

            # Determine source (D-09)
            source = await self._classify_source(project_id, sem_result.branch)

            # Look up rejection history (D-09, D-11)
            rejection_reason = None
            canonical_iri = None
            if source == "rejected":
                rej = await self._get_rejection_info(project_id, sem_result.iri)
                if rej:
                    rejection_reason = rej.rejection_reason
                    canonical_iri = rej.canonical_iri

            candidates.append(
                DuplicateCandidate(
                    iri=sem_result.iri,
                    label=sem_result.label or "",
                    score=round(composite, 4),
                    source=source,
                    branch=sem_result.branch,
                    rejection_reason=rejection_reason,
                    canonical_iri=canonical_iri,
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
        )

    async def _classify_source(self, project_id: UUID, branch: str) -> CandidateSource:
        """Classify a branch as main/pending/rejected per D-09."""
        # Check if branch matches a suggestion session
        session = (
            await self._db.execute(
                select(SuggestionSession)
                .where(
                    SuggestionSession.project_id == project_id,
                    SuggestionSession.branch == branch,
                )
                .order_by(SuggestionSession.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

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

    async def _get_rejection_info(
        self, project_id: UUID, rejected_iri: str
    ) -> DuplicateRejection | None:
        """Look up rejection history for an IRI."""
        result = await self._db.execute(
            select(DuplicateRejection)
            .where(
                DuplicateRejection.project_id == project_id,
                DuplicateRejection.rejected_iri == rejected_iri,
            )
            .order_by(DuplicateRejection.rejected_at.desc())
            .limit(1)
        )
        return result.scalars().first()
