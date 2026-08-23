"""Unit tests for duplicate scoring and distinct-decision suppression."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.schemas.duplicate_check import (
    DistinctDecisionResponse,
    DuplicateCandidate,
    DuplicateCheckResponse,
    ScoreBreakdown,
)
from ontokit.schemas.embeddings import SemanticSearchResultWithBranch
from ontokit.services.duplicate_check_service import (
    BLOCK_THRESHOLD,
    SEMANTIC_WEIGHT,
    STRUCTURAL_WEIGHT,
    WARN_THRESHOLD,
    DuplicateCheckService,
)

PROJECT_ID = uuid4()


def _make_sem_result(
    iri: str = "http://example.org/LegalEntity",
    label: str = "Legal Entity",
    score: float = 1.0,
    branch: str = "main",
) -> SemanticSearchResultWithBranch:
    return SemanticSearchResultWithBranch(
        iri=iri,
        label=label,
        entity_type="class",
        score=score,
        deprecated=False,
        branch=branch,
    )


def _make_service() -> tuple[DuplicateCheckService, MagicMock]:
    """Return a DuplicateCheckService with a mocked AsyncSession."""
    db = MagicMock()
    svc = DuplicateCheckService(db)
    return svc, db


@pytest.mark.asyncio
async def test_exact_label_match_returns_block_verdict():
    """Composite score > 0.95 produces verdict='block' — submission is rejected (DEDUP-05)."""
    svc, _ = _make_service()

    # exact=1.0 (label matches), semantic=1.0, structural=1.0 → composite=1.0 → block
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=1.0,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",  # exact match
            parent_iri="http://example.org/Entity",  # enables structural score
        )

    assert response.verdict == "block"
    assert response.composite_score > BLOCK_THRESHOLD


@pytest.mark.asyncio
async def test_exact_label_blocks_even_when_other_signals_are_weaker():
    """An exact normalized label is deterministic duplicate evidence."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(label="Legal Entity", score=0.8, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.5,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",
            parent_iri="http://example.org/Entity",
        )

    assert response.verdict == "block"
    assert response.composite_score == 1.0


@pytest.mark.asyncio
async def test_below_threshold_passes_silently():
    """Composite score <= 0.80 produces verdict='pass' — no user friction (DEDUP-07)."""
    svc, _ = _make_service()

    # exact=0.0 (different label), semantic=0.5, structural=0.3 → 0+0.2+0.06 = 0.26 → pass
    sem_result = _make_sem_result(label="Completely Different Concept", score=0.5, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.3,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="My New Concept",  # different label → exact_score=0.0
            parent_iri="http://example.org/Entity",
        )

    assert response.verdict == "pass"
    assert response.composite_score <= WARN_THRESHOLD


@pytest.mark.asyncio
async def test_composite_score_weights():
    """Composite = 0.40 * exact + 0.40 * semantic + 0.20 * structural (DEDUP-04, D-01)."""
    svc, _ = _make_service()

    # exact=0.0, semantic=0.5, structural=0.75
    sem_result = _make_sem_result(label="Related Label", score=0.5, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.75,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Target Label",  # exact match → exact_score=1.0
            parent_iri="http://example.org/Entity",
        )

    expected_composite = round(SEMANTIC_WEIGHT * 0.5 + STRUCTURAL_WEIGHT * 0.75, 4)
    assert response.composite_score == expected_composite
    assert response.score_breakdown.exact == 0.0
    assert response.score_breakdown.semantic == 0.5
    assert response.score_breakdown.structural == 0.75


@pytest.mark.asyncio
async def test_missing_structural_signal_renormalizes_available_weights():
    """A minted IRI with no parent can still block on exact + semantic identity."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=0.9, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",
            parent_iri=None,
        )

    assert response.composite_score == 1.0
    assert response.verdict == "block"


@pytest.mark.asyncio
async def test_semantic_only_near_duplicate_can_warn():
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Organization", score=0.86, branch="main")
    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
    ):
        response = await svc.check(PROJECT_ID, "Legal Entity", parent_iri=None)
    assert response.verdict == "warn"
    assert response.composite_score == 0.86


@pytest.mark.asyncio
async def test_all_branch_scope():
    """Duplicate search spans all project branches, not just the active one (DEDUP-08)."""
    svc, _ = _make_service()

    # Candidates from three different branches
    candidates = [
        _make_sem_result(iri="http://ex.org/A", label="Concept A", score=0.9, branch="main"),
        _make_sem_result(
            iri="http://ex.org/B", label="Concept B", score=0.85, branch="suggest-123"
        ),
        _make_sem_result(iri="http://ex.org/C", label="Concept C", score=0.8, branch="suggest-456"),
    ]

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=candidates),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.0,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Any Label",
        )

    response_branches = {c.branch for c in response.candidates}
    assert "main" in response_branches
    assert "suggest-123" in response_branches
    assert "suggest-456" in response_branches
    assert len(response.candidates) == 3


@pytest.mark.asyncio
async def test_rejected_suggestion_source_does_not_conflate_distinct_decision_reason():
    """Suggestion rejection provenance is separate from distinct-pair audit history."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(
        iri="http://ex.org/RejectedEntity",
        label="Some Duplicate Label",
        score=0.95,
        branch="suggest-old",
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.0,
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="rejected")),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Some Other Label",
        )

    assert len(response.candidates) == 1
    candidate = response.candidates[0]
    assert candidate.source == "rejected"
    assert candidate.rejection_reason is None
    assert candidate.canonical_iri is None


@pytest.mark.asyncio
async def test_response_includes_score_breakdown():
    """Response payload contains verdict, composite_score, score_breakdown, and candidates (D-13)."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(score=0.7, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc._structural_svc,
            "try_compute_similarity",
            return_value=0.5,
        ),
        patch.object(
            svc,
            "_classify_source",
            new=AsyncMock(return_value="main"),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Test Label",
            parent_iri="http://example.org/Parent",
        )

    # Check response shape (D-13)
    assert isinstance(response, DuplicateCheckResponse)
    assert response.verdict in ("block", "warn", "pass")
    assert isinstance(response.composite_score, float)
    assert isinstance(response.score_breakdown, ScoreBreakdown)
    assert isinstance(response.score_breakdown.exact, float)
    assert isinstance(response.score_breakdown.semantic, float)
    assert isinstance(response.score_breakdown.structural, float)
    assert isinstance(response.candidates, list)
    assert len(response.candidates) == 1

    candidate = response.candidates[0]
    assert isinstance(candidate, DuplicateCandidate)
    assert isinstance(candidate.iri, str)
    assert isinstance(candidate.label, str)
    assert isinstance(candidate.score, float)
    assert candidate.source in ("main", "pending", "rejected")


@pytest.mark.asyncio
async def test_active_distinct_decision_suppresses_candidate_before_verdict():
    """A matching fingerprint-bound decision removes the candidate from scoring."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="main")
    decision = DuplicateRejection(
        id=uuid4(),
        project_id=PROJECT_ID,
        iri_a="http://example.org/LegalEntity",
        iri_b="http://example.org/ProposedLegalEntity",
        fingerprint_a="a" * 64,
        fingerprint_b="b" * 64,
        reason="These are distinct concepts in this ontology.",
        marked_by="reviewer-1",
        marked_at=datetime.now(UTC),
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            svc,
            "_find_matching_decision",
            new=AsyncMock(return_value=decision),
        ),
    ):
        response = await svc.check(
            PROJECT_ID,
            "Legal Entity",
            proposed_iri="http://example.org/ProposedLegalEntity",
        )

    assert response.verdict == "pass"
    assert response.composite_score == 0.0
    assert response.candidates == []
    assert response.suppressed_decisions == [DistinctDecisionResponse.model_validate(decision)]


@pytest.mark.asyncio
async def test_stale_distinct_decision_does_not_suppress_changed_input():
    """Materially changed normalized inputs resurface the candidate."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(svc, "_classify_source", new=AsyncMock(return_value="main")),
        patch.object(
            svc,
            "_find_matching_decision",
            new=AsyncMock(return_value=None),
        ) as find_decision,
    ):
        response = await svc.check(
            PROJECT_ID,
            "Materially changed legal entity",
            proposed_iri="http://example.org/ProposedLegalEntity",
        )

    assert find_decision.await_count == 1
    assert response.verdict == "block"
    assert [candidate.iri for candidate in response.candidates] == [sem_result.iri]
