"""Unit tests for composite duplicate-check scoring — Plan 04 (DEDUP-04 through DEDUP-08)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ontokit.models.duplicate_rejection import DuplicateRejection
from ontokit.schemas.duplicate_check import (
    DuplicateCandidate,
    DuplicateCheckResponse,
    ScoreBreakdown,
)
from ontokit.schemas.embeddings import SemanticSearchResultWithBranch
from ontokit.services.duplicate_check_service import (
    BLOCK_THRESHOLD,
    EXACT_WEIGHT,
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
    empty_result = MagicMock()
    empty_result.all.return_value = []
    db.execute = AsyncMock(return_value=empty_result)
    svc = DuplicateCheckService(db)
    svc._classify_sources = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _project_id, branches: dict.fromkeys(branches, "main")
    )
    svc._get_rejection_infos = AsyncMock(return_value={})  # type: ignore[method-assign]
    return svc, db


@pytest.mark.asyncio
async def test_exact_label_match_is_blocked_without_embeddings():
    """An exact indexed label remains a hard duplicate when ANN has no rows."""
    svc, db = _make_service()
    exact_row = MagicMock(
        iri="http://example.org/LegalEntity",
        label="Legal Entity",
        entity_type="class",
        branch="feature/exact-match",
        deprecated=False,
    )
    exact_result = MagicMock()
    exact_result.all.return_value = [exact_row]
    db.execute = AsyncMock(return_value=exact_result)

    with patch.object(
        svc._embedding_svc,
        "semantic_search_all_branches",
        new=AsyncMock(return_value=[]),
    ):
        response = await svc.check(PROJECT_ID, "  LEGAL ENTITY  ")

    assert response.verdict == "block"
    assert [(candidate.iri, candidate.branch) for candidate in response.candidates] == [
        ("http://example.org/LegalEntity", "feature/exact-match")
    ]


@pytest.mark.asyncio
async def test_semantic_check_attributes_paid_call_to_requesting_user():
    svc, _ = _make_service()
    svc._billing_user_id = "requesting-user"
    semantic_search = AsyncMock(return_value=[])

    with patch.object(
        svc._embedding_svc,
        "semantic_search_all_branches",
        new=semantic_search,
    ):
        await svc.check(PROJECT_ID, "Candidate")

    assert semantic_search.await_args.kwargs["billing_user_id"] == "requesting-user"


@pytest.mark.asyncio
async def test_rejected_high_score_candidate_does_not_raise_verdict():
    """Rejected matches stay visible but cannot warn or block a new proposal."""
    svc, _ = _make_service()
    sem_result = _make_sem_result(label="Legal Entity", score=1.0, branch="suggest-old")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc,
            "_classify_sources",
            new=AsyncMock(return_value={"suggest-old": "rejected"}),
        ),
    ):
        response = await svc.check(PROJECT_ID, "Legal Entity")

    assert response.verdict == "pass"
    assert response.composite_score == 0.0
    assert response.candidates[0].score > 0.0


@pytest.mark.asyncio
async def test_structural_score_compares_candidate_direct_parent_to_proposed_parent():
    """Structural similarity compares equivalent direct-parent contexts."""
    svc, db = _make_service()
    sem_result = _make_sem_result(label="Different", score=0.5, branch="main")
    hierarchy_row = MagicMock(
        child_iri=sem_result.iri,
        branch="main",
        parent_count=1,
        shares_parent=True,
    )
    exact_result = MagicMock()
    exact_result.all.return_value = []
    hierarchy_result = MagicMock()
    hierarchy_result.all.return_value = [hierarchy_row]
    db.execute = AsyncMock(side_effect=[exact_result, hierarchy_result])

    with patch.object(
        svc._embedding_svc,
        "semantic_search_all_branches",
        new=AsyncMock(return_value=[sem_result]),
    ):
        response = await svc.check(
            PROJECT_ID,
            "New Label",
            parent_iri="http://example.org/Entity",
        )

    assert response.score_breakdown.structural == 1.0


@pytest.mark.asyncio
async def test_check_many_batches_semantic_queries_once():
    """Generation batches provider embeddings while preserving per-label scoring."""
    svc, _ = _make_service()
    semantic_batches = [
        [_make_sem_result(label="Alpha", branch="main")],
        [_make_sem_result(label="Beta", branch="feature/beta")],
    ]
    with patch.object(
        svc._embedding_svc,
        "semantic_search_many_all_branches",
        new=AsyncMock(return_value=semantic_batches),
    ) as batch_search:
        responses = await svc.check_many(
            PROJECT_ID,
            [("Alpha", "http://example.org/Parent"), ("Beta", None)],
        )

    batch_search.assert_awaited_once_with(
        PROJECT_ID,
        ["Alpha", "Beta"],
        limit=10,
        billing_user_id="system:duplicate-check",
    )
    assert len(responses) == 2
    assert all(response.candidates for response in responses)


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
            svc,
            "_load_direct_parent_scores",
            new=AsyncMock(return_value={(sem_result.iri, sem_result.branch): 1.0}),
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
async def test_semantic_similarity_warn_range():
    """Composite score in (0.80, 0.95] produces verdict='warn' — UI surfaces warning (DEDUP-06)."""
    svc, _ = _make_service()

    # exact=1.0, semantic=0.8, structural=0.5 → 0.4+0.32+0.1 = 0.82 → warn
    sem_result = _make_sem_result(label="Legal Entity", score=0.8, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc,
            "_load_direct_parent_scores",
            new=AsyncMock(return_value={(sem_result.iri, sem_result.branch): 0.5}),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Legal Entity",  # exact match → exact_score=1.0
            parent_iri="http://example.org/Entity",
        )

    assert response.verdict == "warn"
    assert WARN_THRESHOLD < response.composite_score <= BLOCK_THRESHOLD


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
            svc,
            "_load_direct_parent_scores",
            new=AsyncMock(return_value={(sem_result.iri, sem_result.branch): 0.3}),
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

    # exact=1.0 (label matches), semantic=0.5, structural=0.75
    # Expected composite = 0.4*1.0 + 0.4*0.5 + 0.2*0.75 = 0.4 + 0.2 + 0.15 = 0.75
    sem_result = _make_sem_result(label="Target Label", score=0.5, branch="main")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc,
            "_load_direct_parent_scores",
            new=AsyncMock(return_value={(sem_result.iri, sem_result.branch): 0.75}),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Target Label",  # exact match → exact_score=1.0
            parent_iri="http://example.org/Entity",
        )

    expected_composite = round(
        EXACT_WEIGHT * 1.0 + SEMANTIC_WEIGHT * 0.5 + STRUCTURAL_WEIGHT * 0.75, 4
    )
    assert response.composite_score == expected_composite
    assert response.score_breakdown.exact == 1.0
    assert response.score_breakdown.semantic == 0.5
    assert response.score_breakdown.structural == 0.75


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
async def test_same_iri_on_different_branches_preserves_both_candidates():
    """Cross-branch identity includes branch, so projections are never mixed."""
    svc, _ = _make_service()
    iri = "http://example.org/Shared"
    semantic = _make_sem_result(iri=iri, label="Semantic Label", score=0.7, branch="main")
    exact = _make_sem_result(iri=iri, label="Exact Label", score=0.0, branch="feature/exact")

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[semantic]),
        ),
        patch.object(
            svc,
            "_find_exact_label_matches",
            new=AsyncMock(return_value=[exact]),
        ),
    ):
        response = await svc.check(PROJECT_ID, "Exact Label")

    assert {(candidate.iri, candidate.branch) for candidate in response.candidates} == {
        (iri, "main"),
        (iri, "feature/exact"),
    }
    assert response.verdict == "block"


@pytest.mark.asyncio
async def test_rejection_history_surfaced():
    """Previously-rejected candidates include rejection_reason in the response (D-09)."""
    svc, _ = _make_service()

    sem_result = _make_sem_result(
        iri="http://ex.org/RejectedEntity",
        label="Some Duplicate Label",
        score=0.95,
        branch="suggest-old",
    )

    rej_record = DuplicateRejection(
        project_id=PROJECT_ID,
        rejected_iri="http://ex.org/RejectedEntity",
        canonical_iri="http://ex.org/CanonicalEntity",
        rejection_reason="Duplicate of Canonical Entity",
        rejected_by="user-id-123",
    )

    with (
        patch.object(
            svc._embedding_svc,
            "semantic_search_all_branches",
            new=AsyncMock(return_value=[sem_result]),
        ),
        patch.object(
            svc,
            "_classify_sources",
            new=AsyncMock(return_value={"suggest-old": "rejected"}),
        ),
        patch.object(
            svc,
            "_get_rejection_infos",
            new=AsyncMock(return_value={sem_result.iri: rej_record}),
        ),
    ):
        response = await svc.check(
            project_id=PROJECT_ID,
            label="Some Other Label",
        )

    assert len(response.candidates) == 1
    candidate = response.candidates[0]
    assert candidate.source == "rejected"
    assert candidate.rejection_reason == "Duplicate of Canonical Entity"
    assert candidate.canonical_iri == "http://ex.org/CanonicalEntity"


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
            svc,
            "_load_direct_parent_scores",
            new=AsyncMock(return_value={(sem_result.iri, sem_result.branch): 0.5}),
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
