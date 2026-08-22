"""Composite duplicate detection service — exact + semantic + structural scoring."""

import logging
from uuid import UUID

from sqlalchemy import case, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.models.ontology_index import IndexedEntity, IndexedHierarchy, IndexedLabel
from ontokit.models.suggestion_session import SuggestionSession, SuggestionSessionStatus
from ontokit.schemas.duplicate_check import (
    CandidateSource,
    DuplicateCandidate,
    DuplicateCheckResponse,
    DuplicateVerdict,
    ScoreBreakdown,
)
from ontokit.schemas.embeddings import SemanticSearchResultWithBranch
from ontokit.services.embedding_service import EmbeddingService

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

    async def check(
        self,
        project_id: UUID,
        label: str,
        entity_type: str = "class",
        parent_iri: str | None = None,
        limit: int = 10,
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

        # Exact labels are an independent indexed signal: an unavailable, empty,
        # or stale embedding projection must never hide a definitive duplicate.
        exact_candidates = await self._find_exact_label_matches(project_id, normalized_label, limit)
        try:
            semantic_candidates = await self._embedding_svc.semantic_search_all_branches(
                project_id, label, limit=limit
            )
        except Exception as exc:
            logger.warning(
                "Semantic duplicate search unavailable; continuing with exact labels: %s",
                exc,
            )
            semantic_candidates = []

        return await self._score_candidates(
            project_id,
            normalized_label=normalized_label,
            exact_candidates=exact_candidates,
            semantic_candidates=semantic_candidates,
            parent_iri=parent_iri,
            limit=limit,
        )

    async def _score_candidates(
        self,
        project_id: UUID,
        *,
        normalized_label: str,
        exact_candidates: list[SemanticSearchResultWithBranch],
        semantic_candidates: list[SemanticSearchResultWithBranch],
        parent_iri: str | None,
        limit: int,
    ) -> DuplicateCheckResponse:
        """Score already-loaded exact and semantic candidates for one label."""

        # Merge the two bounded sources by ontology identity. Exact-index data
        # wins for the label/branch projection; ANN still contributes its score.
        semantic_keys = {(candidate.iri, candidate.branch) for candidate in semantic_candidates}
        candidates_by_key = {
            (candidate.iri, candidate.branch): candidate for candidate in semantic_candidates
        }
        for exact_candidate in exact_candidates:
            candidate_key = (exact_candidate.iri, exact_candidate.branch)
            semantic_candidate = candidates_by_key.get(candidate_key)
            if semantic_candidate is None:
                candidates_by_key[candidate_key] = exact_candidate
            else:
                candidates_by_key[candidate_key] = semantic_candidate.model_copy(
                    update={
                        "label": exact_candidate.label,
                        "entity_type": exact_candidate.entity_type,
                        "deprecated": exact_candidate.deprecated,
                    }
                )
        merged_candidates = list(candidates_by_key.values())

        if not merged_candidates:
            return DuplicateCheckResponse(
                verdict="pass",
                composite_score=0.0,
                score_breakdown=ScoreBreakdown(exact=0.0, semantic=0.0, structural=0.0),
                candidates=[],
            )

        parent_scores = (
            await self._load_direct_parent_scores(project_id, merged_candidates, parent_iri)
            if parent_iri
            else {}
        )
        source_by_branch = await self._classify_sources(
            project_id, {candidate.branch for candidate in merged_candidates}
        )
        rejected_iris = {
            candidate.iri
            for candidate in merged_candidates
            if source_by_branch[candidate.branch] == "rejected"
        }
        rejection_by_iri = await self._get_rejection_infos(project_id, rejected_iris)

        # 2. Build candidate list with all three scores
        candidates: list[DuplicateCandidate] = []
        best_composite = 0.0
        best_breakdown = ScoreBreakdown(exact=0.0, semantic=0.0, structural=0.0)
        actionable_exact_fallback = False

        for sem_result in merged_candidates:
            # Exact score: case-insensitive label match
            candidate_label_norm = (sem_result.label or "").lower().strip()
            exact_score = 1.0 if candidate_label_norm == normalized_label else 0.0

            # Semantic score: from ANN search
            semantic_score = sem_result.score

            # Structural score compares like with like: the candidate's direct
            # parents against the proposed entity's direct-parent context.
            structural_score = parent_scores.get((sem_result.iri, sem_result.branch), 0.0)

            # Composite (D-01 weights)
            composite = (
                EXACT_WEIGHT * exact_score
                + SEMANTIC_WEIGHT * semantic_score
                + STRUCTURAL_WEIGHT * structural_score
            )

            # Determine source (D-09)
            source = source_by_branch[sem_result.branch]

            # Look up rejection history (D-09, D-11)
            rejection_reason = None
            canonical_iri = None
            if source == "rejected":
                rej = rejection_by_iri.get(sem_result.iri)
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

            # A dismissed/rejected candidate remains useful historical context,
            # but it cannot affect the current warn/block decision.
            if (
                source != "rejected"
                and exact_score == 1.0
                and (sem_result.iri, sem_result.branch) not in semantic_keys
            ):
                actionable_exact_fallback = True
            if source != "rejected" and composite > best_composite:
                best_composite = composite
                best_breakdown = ScoreBreakdown(
                    exact=round(exact_score, 4),
                    semantic=round(semantic_score, 4),
                    structural=round(structural_score, 4),
                )

        # Sort candidates by score descending
        candidates.sort(key=lambda c: c.score, reverse=True)
        candidates = candidates[:limit]

        # Determine verdict (D-02)
        if actionable_exact_fallback or best_composite > BLOCK_THRESHOLD:
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

    async def check_many(
        self,
        project_id: UUID,
        checks: list[tuple[str, str | None]],
        limit: int = 10,
    ) -> list[DuplicateCheckResponse]:
        """Check several labels with one embedding-provider batch call."""
        if not checks:
            return []
        labels = [label for label, _parent_iri in checks]
        try:
            semantic_batches = await self._embedding_svc.semantic_search_many_all_branches(
                project_id, labels, limit=limit
            )
            if len(semantic_batches) != len(checks):
                raise RuntimeError("Semantic search returned an unexpected batch size")
        except Exception as exc:
            logger.warning(
                "Semantic duplicate batch search unavailable; continuing with exact labels: %s",
                exc,
            )
            semantic_batches = [[] for _ in checks]

        responses: list[DuplicateCheckResponse] = []
        for (label, parent_iri), semantic_candidates in zip(checks, semantic_batches, strict=True):
            normalized_label = label.lower().strip()
            exact_candidates = await self._find_exact_label_matches(
                project_id, normalized_label, limit
            )
            responses.append(
                await self._score_candidates(
                    project_id,
                    normalized_label=normalized_label,
                    exact_candidates=exact_candidates,
                    semantic_candidates=semantic_candidates,
                    parent_iri=parent_iri,
                    limit=limit,
                )
            )
        return responses

    async def _find_exact_label_matches(
        self,
        project_id: UUID,
        normalized_label: str,
        limit: int,
    ) -> list[SemanticSearchResultWithBranch]:
        """Return a bounded, cross-branch exact-label projection from the index."""
        if not normalized_label:
            return []

        result = await self._db.execute(
            select(
                IndexedEntity.iri,
                func.min(IndexedLabel.value).label("label"),
                IndexedEntity.entity_type,
                IndexedEntity.branch,
                IndexedEntity.deprecated,
            )
            .join(IndexedLabel, IndexedLabel.entity_id == IndexedEntity.id)
            .where(
                IndexedEntity.project_id == project_id,
                func.lower(func.trim(IndexedLabel.value)) == normalized_label,
            )
            .group_by(
                IndexedEntity.iri,
                IndexedEntity.entity_type,
                IndexedEntity.branch,
                IndexedEntity.deprecated,
            )
            .order_by(
                case((IndexedEntity.branch == "main", 0), else_=1),
                IndexedEntity.iri,
                IndexedEntity.branch,
            )
            .limit(limit)
        )
        return [
            SemanticSearchResultWithBranch(
                iri=row.iri,
                label=row.label,
                entity_type=row.entity_type,
                score=0.0,
                branch=row.branch,
                deprecated=row.deprecated,
            )
            for row in result.all()
        ]

    async def _load_direct_parent_scores(
        self,
        project_id: UUID,
        candidates: list[SemanticSearchResultWithBranch],
        proposed_parent_iri: str,
    ) -> dict[tuple[str, str], float]:
        """Compute bounded direct-parent Jaccard scores in one aggregate query."""
        candidate_keys = {(candidate.iri, candidate.branch) for candidate in candidates}
        if not candidate_keys:
            return {}

        result = await self._db.execute(
            select(
                IndexedHierarchy.child_iri,
                IndexedHierarchy.branch,
                func.count(IndexedHierarchy.parent_iri).label("parent_count"),
                func.bool_or(IndexedHierarchy.parent_iri == proposed_parent_iri).label(
                    "shares_parent"
                ),
            )
            .where(
                IndexedHierarchy.project_id == project_id,
                tuple_(IndexedHierarchy.child_iri, IndexedHierarchy.branch).in_(
                    sorted(candidate_keys)
                ),
            )
            .group_by(IndexedHierarchy.child_iri, IndexedHierarchy.branch)
        )
        return {
            (row.child_iri, row.branch): 1.0 / int(row.parent_count)
            for row in result.all()
            if row.shares_parent and row.parent_count
        }

    async def _classify_sources(
        self, project_id: UUID, branches: set[str]
    ) -> dict[str, CandidateSource]:
        """Classify bounded branches from their latest suggestion session."""
        ranked_sessions = (
            select(
                SuggestionSession.branch,
                SuggestionSession.status,
                func.row_number()
                .over(
                    partition_by=SuggestionSession.branch,
                    order_by=(SuggestionSession.created_at.desc(), SuggestionSession.id.desc()),
                )
                .label("rank"),
            )
            .where(
                SuggestionSession.project_id == project_id,
                SuggestionSession.branch.in_(sorted(branches)),
            )
            .subquery()
        )
        result = await self._db.execute(
            select(ranked_sessions.c.branch, ranked_sessions.c.status).where(
                ranked_sessions.c.rank == 1
            )
        )
        sources: dict[str, CandidateSource] = dict.fromkeys(branches, "main")
        for row in result.all():
            if row.status == SuggestionSessionStatus.REJECTED.value:
                sources[row.branch] = "rejected"
            elif row.status in (
                SuggestionSessionStatus.ACTIVE.value,
                SuggestionSessionStatus.SUBMITTED.value,
                SuggestionSessionStatus.AUTO_SUBMITTED.value,
                SuggestionSessionStatus.CHANGES_REQUESTED.value,
            ):
                sources[row.branch] = "pending"
        return sources

    async def _get_rejection_infos(
        self, project_id: UUID, rejected_iris: set[str]
    ) -> dict[str, DuplicateRejection]:
        """Bulk-load the latest rejection history for bounded candidate IRIs."""
        if not rejected_iris:
            return {}
        ranked_rejections = (
            select(
                DuplicateRejection.id,
                func.row_number()
                .over(
                    partition_by=DuplicateRejection.rejected_iri,
                    order_by=(
                        DuplicateRejection.rejected_at.desc(),
                        DuplicateRejection.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(
                DuplicateRejection.project_id == project_id,
                DuplicateRejection.rejected_iri.in_(sorted(rejected_iris)),
            )
            .subquery()
        )
        result = await self._db.execute(
            select(DuplicateRejection)
            .join(ranked_rejections, ranked_rejections.c.id == DuplicateRejection.id)
            .where(ranked_rejections.c.rank == 1)
        )
        return {row.rejected_iri: row for row in result.scalars().all()}
